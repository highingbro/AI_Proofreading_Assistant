"""分层结果落库（阶段6实现，阶段7新增 context_snippet 计算）。"""

from __future__ import annotations

import config
from core.classifier import ClassifiedResult
from core.parser import ParsedDocument
from db.models import add_issue, create_record


def _build_context_snippet(parsed: ParsedDocument, block_index: int) -> str:
    """取 block_index 前后各 config.FOLLOWUP_CONTEXT_WINDOW_BLOCKS 个block的文本拼接。

    直接用列表下标而不线性扫描 block_index 字段：core/parser 无论走PDF原生/OCR/Word
    哪条通道，都是按阅读顺序把 ParsedBlock append 进 blocks 列表的同时用同一个递增计数器
    赋 block_index，所以 parsed.blocks[i].block_index == i 恒成立。
    """
    window = config.FOLLOWUP_CONTEXT_WINDOW_BLOCKS
    lo = max(0, block_index - window)
    hi = min(len(parsed.blocks) - 1, block_index + window)
    return "\n".join(b.text for b in parsed.blocks[lo : hi + 1])


def persist_result(
    result: ClassifiedResult,
    doc_name: str,
    doc_version: str = "",
    task_type: str = "标准校对",
    parsed: ParsedDocument | None = None,
    mode: str = config.PROOFREAD_MODE_DEEP,
    db_path=None,
) -> tuple[int, list[int]]:
    """把分层结果落库：先建 record（分层统计取自 result.stats），再逐条 add_issue。

    parsed 非空时，为每条已定位（block_index非None）的issue计算 context_snippet
    （原文前后文窗口，阶段7追问用）；不传 parsed 或issue未定位时 context_snippet 留空，
    与阶段6原行为一致。

    mode 记录本次校对实际使用的模式（精简/深度），写入 records.mode 供历史记录页展示。

    返回 (record_id, issue_ids)，issue_ids 与 result.issues 顺序一一对应。
    """
    record_id = create_record(
        doc_name=doc_name,
        doc_version=doc_version,
        task_type=task_type,
        total_issues=result.stats.get("total_issues", 0),
        count_confirmed=result.stats.get("count_confirmed", 0),
        count_doubtful=result.stats.get("count_doubtful", 0),
        count_quotation=result.stats.get("count_quotation", 0),
        count_optional=result.stats.get("count_optional", 0),
        high_priority_count=result.stats.get("high_priority_count", 0),
        mode=mode,
        db_path=db_path,
    )

    issue_ids = []
    for issue in result.issues:
        context_snippet = None
        if parsed is not None and issue.block_index is not None:
            context_snippet = _build_context_snippet(parsed, issue.block_index)
        issue_id = add_issue(
            record_id=record_id,
            page_location=issue.page_location,
            original_text=issue.original_text,
            issue_type=issue.issue_type,
            priority=issue.priority,
            layer=issue.layer,
            suggestion=issue.suggestion,
            context_snippet=context_snippet,
            db_path=db_path,
        )
        issue_ids.append(issue_id)

    return record_id, issue_ids
