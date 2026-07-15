"""标准校对全链路编排（阶段6实现）。"""

from __future__ import annotations

import config
from core.chunker import chunk_document
from core.classifier import ClassifiedResult, classify_issues
from core.parser import ParsedDocument, parse_document
from core.proofreader import proofread_document


def run_standard_proofread(
    file_path, progress_callback=None, mode: str = config.PROOFREAD_MODE_DEEP
) -> tuple[ClassifiedResult, ParsedDocument]:
    """标准校对全链路：解析→分块→逐块LLM校对→结果分层。纯编排，不写库。

    返回 (ClassifiedResult, ParsedDocument) 而非阶段6原先的单个 ClassifiedResult——
    parsed 要传给 persist_result 用于计算 context_snippet（阶段7新增），ParsedDocument
    只在本次调用链上存在，过后无法从数据库反查，必须在这里一并交出去。

    mode 透传给 proofread_document 控制校对规则子集（精简/深度），默认深度模式，
    与该参数新增前的行为一致。
    """
    parsed = parse_document(file_path)
    chunked = chunk_document(parsed)
    proofread_result = proofread_document(chunked, progress_callback=progress_callback, mode=mode)
    return classify_issues(proofread_result, parsed, chunked), parsed
