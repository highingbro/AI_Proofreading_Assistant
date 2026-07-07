"""LLM 调用封装（阶段4实现）。

负责封装大模型 API 调用（含重试），并接入校对提示词
（含引文保护、事实置信度分级两条补充规则）。本阶段仅留函数签名。
"""


def call_llm(prompt: str) -> str:
    """调用 LLM，返回原始文本响应。"""
    raise NotImplementedError


def proofread_chunk(chunk: dict) -> list[dict]:
    """对单个文本块执行校对，返回结构化问题列表。"""
    raise NotImplementedError
