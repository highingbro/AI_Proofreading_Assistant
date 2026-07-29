"""分层结果落库（含 context_snippet 计算）。"""

from __future__ import annotations

import config
from core.classifier import ClassifiedResult
from core.parser import ParsedDocument
from db.models import add_issue, create_record


def build_context_snippet(parsed: ParsedDocument, block_index: int) -> str:
    """取 block_index 前后各 config.FOLLOWUP_CONTEXT_WINDOW_BLOCKS 个block的文本拼接。

    直接用列表下标而不线性扫描 block_index 字段：core/parser 无论走PDF原生/OCR/Word
    哪条通道，都是按阅读顺序把 ParsedBlock append 进 blocks 列表的同时用同一个递增计数器
    赋 block_index，所以 parsed.blocks[i].block_index == i 恒成立。

    原为 persist_result 私有（_build_context_snippet），因为原稿比对的
    persist_comparison_result 同样需要复用这份"取上下文窗口"逻辑，提升为公开函数，
    不重复实现。
    """
    window = config.FOLLOWUP_CONTEXT_WINDOW_BLOCKS
    lo = max(0, block_index - window)
    hi = min(len(parsed.blocks) - 1, block_index + window)
    return "\n".join(b.text for b in parsed.blocks[lo : hi + 1])


def persist_result(
    result: ClassifiedResult,
    task_id: int,
    doc_name: str,
    doc_version: str = "",
    task_type: str = "标准校对",
    parsed: ParsedDocument | None = None,
    mode: str = config.PROOFREAD_MODE_DEEP,
    author: str | None = None,
    db_path=None,
) -> tuple[int, list[int]]:
    """把分层结果落库：先建 record（分层统计取自 result.stats），再逐条 add_issue。

    task_id 必传——每条记录都归属于某个任务，见 db/models.py::create_record。
    author 是本轮校对的署名（谁做的），选填。

    parsed 非空时，为每条已定位（block_index非None）的issue计算 context_snippet
    （原文前后文窗口，供追问使用）；不传 parsed 或issue未定位时 context_snippet 留空。

    mode 记录本次校对实际使用的模式（精简/深度），写入 records.mode 供历史记录页展示。

    返回 (record_id, issue_ids)，issue_ids 与 result.issues 顺序一一对应。
    """
    record_id = create_record(
        task_id=task_id,
        author=author,
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
            context_snippet = build_context_snippet(parsed, issue.block_index)
        issue_id = add_issue(
            record_id=record_id,
            page_location=issue.page_location,
            original_text=issue.original_text,
            issue_type=issue.issue_type,
            priority=issue.priority,
            layer=issue.layer,
            suggestion=issue.suggestion,
            context_snippet=context_snippet,
            doc_page=issue.doc_page,
            db_path=db_path,
        )
        issue_ids.append(issue_id)

    return record_id, issue_ids


def persist_comparison_result(
    diffs: list[dict],
    task_id: int,
    doc_name: str,
    doc_version: str = "",
    formatted: ParsedDocument | None = None,
    author: str | None = None,
    db_path=None,
) -> tuple[int, list[int]]:
    """把原稿比对（core.comparer.compare_documents）产出的差异条目落库。

    复用 records/issues 两张通用表，不新建表结构——task_type 固定为"原稿比对"，
    issue_type/layer 填差异特有的值（"新增内容"/"删除内容"/"文字替换"，层级恒为
    config.DIFF_LAYER_SUBSTANTIVE，因为 compare_documents 已经把归一化后相同的纯
    排版差异过滤掉了）。priority 固定为 config.PRIORITY_MEDIUM，比对场景不像标准
    校对那样需要区分优先级。

    formatted 非空时用 build_context_snippet 计算每条已定位差异（diff['block_index']
    非None）的上下文——与 persist_result 是同一份逻辑，diff['block_index'] 取自差异
    条目在排版稿里对应的 ParsedBlock.block_index（纯新增的条目没有对应的原稿block，
    block_index 为 None，context_snippet 留空）。

    返回 (record_id, issue_ids)，issue_ids 与 diffs 顺序一一对应。
    """
    record_id = create_record(
        task_id=task_id,
        author=author,
        doc_name=doc_name,
        doc_version=doc_version,
        task_type="原稿比对",
        total_issues=len(diffs),
        db_path=db_path,
    )

    issue_ids = []
    for diff in diffs:
        context_snippet = None
        if formatted is not None and diff.get("block_index") is not None:
            context_snippet = build_context_snippet(formatted, diff["block_index"])
        issue_id = add_issue(
            record_id=record_id,
            page_location=diff["page_location"],
            original_text=diff["original_text"],
            issue_type=diff["diff_type"],
            priority=config.PRIORITY_MEDIUM,
            layer=diff["layer"],
            suggestion=diff["suggestion"],
            context_snippet=context_snippet,
            doc_page=diff.get("doc_page"),
            db_path=db_path,
        )
        issue_ids.append(issue_id)

    return record_id, issue_ids
