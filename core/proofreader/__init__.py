"""校对提示词组装与单块/全文档校对。

把 core/chunker/ 产出的 Chunk 送入 LLM 校对，解析/校验/容错 LLM 的 JSON
输出，并把每条问题回填定位到原始 block_index/page_location。本模块只输出
LLM自报的结构化判断（category/confidence 未经强制归层校验），core/classifier/
负责按规则做归层强制校验。

详细设计背景（提示词原文的调整、动态超时估算、并发执行设计决策）
见 core/proofreader/CLAUDE.md。
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
from core.chunker import Chunk, ChunkedDocument, locate_block, locate_doc_page
from core.llm_client import LLMCallError, chat_completion
from core.parser import ParsedDocument
from core.proofreader._types import LLMResponseError, ProofreadResult, RawIssue
from core.proofreader.locator import _contains, _locate_block_for_snippet, _split_body_overlap
from core.proofreader.prompt_builder import _build_system_prompt, _RULES_PATH, _split_rules_by_number
from core.proofreader.response_parser import _parse_json_array, _validate_item

__all__ = ["RawIssue", "ProofreadResult", "LLMResponseError", "proofread_chunk", "proofread_document"]

logger = logging.getLogger(__name__)

_RETRY_HINT = "你上次输出不是合法JSON，只输出JSON数组。"


# ---------------------------------------------------------------------------
# 单块 / 全文档校对
# ---------------------------------------------------------------------------

def proofread_chunk(
    chunk: Chunk,
    parsed: ParsedDocument,
    mode: str = config.PROOFREAD_MODE_DEEP,
    rejection_rules_text: str = "",
) -> list[RawIssue]:
    """对单个文本块执行校对，返回结构化问题列表。

    注：多接一个 parsed: ParsedDocument 参数——Chunk 本身不持有到 ParsedDocument
    的反向引用，而 RawIssue.page_location 需要调用 locate_block(parsed, block_index)
    才能得到。proofread_document 内部会用 chunked.source 传入。

    mode 控制注入LLM的校对规则子集（config.PROOFREAD_MODE_DEEP/PROOFREAD_MODE_SIMPLIFIED），
    默认深度模式。

    rejection_rules_text：core/feedback_rules.py::load_rejection_rules_text() 产出的
    历史反馈总结规则文本（默认空字符串=没有这份参照），让LLM在生成建议这一步就规避
    曾被人工拒绝的问题模式（详见 core/feedback_rules.py 模块docstring）。
    """
    system_prompt = _build_system_prompt(mode, rejection_rules_text)
    body_text, overlap_text = _split_body_overlap(chunk.text)

    user_content = chunk.text
    raw_items: list[dict] | None = None
    raw_response = ""
    for retry in range(2):
        raw_response = chat_completion(system_prompt, user_content)
        try:
            raw_items = _parse_json_array(raw_response)
            break
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("第%d块LLM输出解析失败(第%d次): %s", chunk.chunk_index, retry + 1, exc)
            user_content = chunk.text + "\n\n" + _RETRY_HINT

    if raw_items is None:
        logger.error("第%d块LLM输出两次均无法解析为JSON，原始回复: %s", chunk.chunk_index, raw_response)
        raise LLMResponseError(f"第{chunk.chunk_index}块LLM输出无法解析为合法JSON", raw_response=raw_response)

    issues: list[RawIssue] = []
    for item in raw_items:
        if not _validate_item(item):
            logger.warning("第%d块丢弃不合法条目: %s", chunk.chunk_index, item)
            continue

        original_text = item["original_text"]

        if _contains(body_text, original_text):
            block_index = _locate_block_for_snippet(original_text, chunk, parsed)
            located = block_index is not None
            page_location = locate_block(parsed, block_index) if located else None
            doc_page = locate_doc_page(parsed, block_index) if located else None
            if not located:
                logger.warning("第%d块条目命中正文但未能归属到具体block: %s", chunk.chunk_index, original_text)
        elif overlap_text and _contains(overlap_text, original_text):
            logger.info("第%d块丢弃仅出现在重叠区的条目: %s", chunk.chunk_index, original_text)
            continue
        else:
            block_index = None
            located = False
            page_location = None
            doc_page = None
            logger.warning("第%d块条目未能定位: %s", chunk.chunk_index, original_text)

        issues.append(
            RawIssue(
                original_text=original_text,
                issue_type=item["issue_type"],
                category=item["category"],
                confidence=item["confidence"],
                suggestion=item["suggestion"],
                reason=item["reason"],
                block_index=block_index,
                page_location=page_location,
                chunk_index=chunk.chunk_index,
                located=located,
                doc_page=doc_page,
            )
        )

    return issues


def proofread_document(
    chunked: ChunkedDocument,
    progress_callback=None,
    mode: str = config.PROOFREAD_MODE_DEEP,
    rejection_rules_text: str = "",
) -> ProofreadResult:
    """并发调用 proofread_chunk 校对所有 chunk，单块失败不中断其余块。

    并发数上限 config.PROOFREAD_MAX_CONCURRENT_CHUNKS，块数不足该值时相当于
    全部并发。账号RPM/TPM额度虽够用，但 data/app.log 真实数据显示瓶颈是模型
    服务端实际并发处理能力，不设上限会导致请求数远超服务端承载后集体排队、
    单块耗时被拖到超时上限，失败重试还会把同样的拥堵重演一次——详见
    core/proofreader/CLAUDE.md。chat_completion/proofread_chunk 内部没有共享
    可变状态（每次调用用的都是局部变量），天然线程安全，不需要额外加锁。

    mode 透传给每个 proofread_chunk 调用，控制校对规则子集，默认深度模式。
    rejection_rules_text 同样透传，只在校对开始前读一次
    （core/workflow/run.py 调用 load_rejection_rules_text），所有chunk共享。
    """
    total = len(chunked.chunks)
    if total == 0:
        return ProofreadResult()

    issues_by_index: dict[int, list[RawIssue]] = {}
    warnings_by_index: dict[int, str] = {}

    with ThreadPoolExecutor(max_workers=min(total, config.PROOFREAD_MAX_CONCURRENT_CHUNKS)) as executor:
        future_to_index = {
            executor.submit(
                proofread_chunk, chunk, chunked.source, mode, rejection_rules_text
            ): (i, chunk)
            for i, chunk in enumerate(chunked.chunks)
        }
        # as_completed 本身在调用方（主线程）里顺序迭代，循环体不并发执行，
        # 不存在 completed 计数的竞态，不需要加锁。
        for completed, future in enumerate(as_completed(future_to_index), start=1):
            i, chunk = future_to_index[future]
            try:
                issues_by_index[i] = future.result()
            except (LLMCallError, LLMResponseError) as exc:
                msg = f"第{chunk.chunk_index}块校对失败: {exc}"
                logger.error(msg)
                warnings_by_index[i] = msg
            if progress_callback is not None:
                progress_callback(completed, total)

    result = ProofreadResult()
    for i in range(total):
        result.issues.extend(issues_by_index.get(i, []))
        if i in warnings_by_index:
            result.chunk_warnings.append(warnings_by_index[i])
    return result
