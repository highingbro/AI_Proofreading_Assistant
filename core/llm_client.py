"""LLM 调用封装。

封装大模型 API 调用（DashScope 兼容模式 REST 接口：POST {base_url}/chat/completions），
处理网络错误/限流/5xx 的指数退避重试。不做流式；本函数自身不发起并发请求，
但每次调用只用局部变量、无共享可变状态，天然线程安全——
core.proofreader.proofread_document 会用线程池并发调用本函数校对多个chunk。

`chat_completion()` 是唯一的LLM调用入口，网络错误/超时/429/5xx 按 1s/4s/16s
指数退避重试（config.LLM_MAX_RETRIES），429 优先遵守 Retry-After 响应头；
其余4xx判定不可重试直接失败。config.LLM_BASE_URL/LLM_MODEL 都带默认值，仍
支持同名环境变量覆盖；只有 LLM_API_KEY（读取环境变量 DASHSCOPE_API_KEY，
注意变量名不对称）是真正必填项，缺失会在调用时抛 LLMCallError，不会在
`import config` 时就报错。

几个非显而易见的行为点：

1. LLM_API_KEY 实际读取环境变量 DASHSCOPE_API_KEY（阿里云DashScope标准命名），
   不是字面的 LLM_API_KEY——config.py 里属性名仍叫 LLM_API_KEY，只是内部命名，
   读取源不同。这是唯一没有默认值、真正必填的一项。
2. LLM_MODEL 默认值 "deepseek-v3.2"，不强制要求设置环境变量，仍支持该环境
   变量覆盖换模型。
3. LLM_BASE_URL 默认是 DashScope 兼容模式公开固定地址
   （https://dashscope.aliyuncs.com/compatible-mode/v1），同样支持环境变量
   覆盖。
4. LLM_TIMEOUT 默认固定为 config.LLM_TIMEOUT_FIXED_SECONDS——详见下方专门一段。
5. LLM调用用 requests 直接发REST请求，没有引入 openai/dashscope SDK：避免
   额外SDK依赖与版本兼容负担，REST接口本身足够简单直接（见 requirements.txt
   里的注释）。

超时固定为 config.LLM_TIMEOUT_FIXED_SECONDS（900秒），不随文本长度浮动——
起因：真实文档大小的chunk（约3000~5000字总输入）用固定120秒超时会稳定超时
失败（4次重试全部撞线），而实际耗时普遍在180~212秒；曾按文本长度动态估算，
但真实模型耗时经常逼近/超过估算值，反而提前触发本可避免的重试，改为统一
固定值更宽松（详见 config.py 里的注释）。优先级：显式传 timeout= 参数 >
环境变量 LLM_TIMEOUT（一旦设置就固定用它）> LLM_TIMEOUT_FIXED_SECONDS。
"""

from __future__ import annotations

import logging
import time

import requests

import config

logger = logging.getLogger(__name__)

# 固定退避序列（秒），对应提示词要求的 1s/4s/16s；超出序列长度的重试用最后一档兜底。
_BACKOFF_SECONDS = (1, 4, 16)


class LLMCallError(Exception):
    """LLM调用失败（配置缺失、重试耗尽、或不可重试的4xx错误），message带最后一次错误信息。"""


def _check_config() -> None:
    missing = [
        name
        for name, value in (
            ("LLM_BASE_URL", config.LLM_BASE_URL),
            ("LLM_API_KEY(即环境变量DASHSCOPE_API_KEY)", config.LLM_API_KEY),
        )
        if not value
    ]
    if missing:
        raise LLMCallError(f"缺少必要的环境变量: {', '.join(missing)}")


def _backoff_delay(attempt_index: int) -> float:
    if attempt_index < len(_BACKOFF_SECONDS):
        return _BACKOFF_SECONDS[attempt_index]
    return _BACKOFF_SECONDS[-1]


def chat_completion(
    system_prompt: str,
    user_content: str,
    *,
    temperature: float | None = None,
    timeout: int | None = None,
) -> str:
    """单轮调用，返回助手回复文本。内部处理重试与错误。"""
    _check_config()

    temperature = config.LLM_TEMPERATURE if temperature is None else temperature
    if timeout is None:
        # 显式传参 > LLM_TIMEOUT 环境变量固定值 > LLM_TIMEOUT_FIXED_SECONDS
        timeout = config.LLM_TIMEOUT if config.LLM_TIMEOUT is not None else config.LLM_TIMEOUT_FIXED_SECONDS
    max_retries = config.LLM_MAX_RETRIES

    url = f"{config.LLM_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
    }

    last_error = "未知错误"
    for attempt in range(max_retries + 1):
        start = time.monotonic()
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            last_error = f"网络错误: {exc}"
            logger.warning("LLM调用失败(第%d次尝试): %s", attempt + 1, last_error)
            if attempt < max_retries:
                time.sleep(_backoff_delay(attempt))
            continue

        if resp.status_code == 200:
            elapsed = time.monotonic() - start
            content = resp.json()["choices"][0]["message"]["content"]
            logger.info(
                "LLM调用成功 耗时=%.2fs 输入字符数=%d 输出字符数=%d 重试次数=%d 本次超时上限=%ds",
                elapsed,
                len(system_prompt) + len(user_content),
                len(content),
                attempt,
                timeout,
            )
            return content

        if resp.status_code == 429 or resp.status_code >= 500:
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
            logger.warning("LLM调用失败(第%d次尝试): %s", attempt + 1, last_error)
            if attempt < max_retries:
                delay = None
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = float(retry_after)
                        except ValueError:
                            delay = None
                if delay is None:
                    delay = _backoff_delay(attempt)
                time.sleep(delay)
            continue

        # 其他4xx（如401/400）：请求本身有问题，重试无意义，直接失败
        last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        logger.error("LLM调用失败(不可重试): %s", last_error)
        raise LLMCallError(last_error)

    raise LLMCallError(f"重试{max_retries}次后仍失败: {last_error}")
