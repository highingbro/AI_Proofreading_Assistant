"""单条问题的状态/批注更新（阶段6实现，阶段8"批注"补丁新增 set_issue_note）。"""

from __future__ import annotations

from db.models import get_issues, update_issue_note, update_issue_status, update_record_stats


def set_issue_status(
    issue_id: int,
    status: str,
    record_id: int | None = None,
    db_path=None,
) -> None:
    """更新单条问题状态；若给了 record_id，顺带把该 record 的采纳/拒绝计数重算并写回。

    批注（note）与采纳/拒绝状态无关，不在这里处理，见 set_issue_note。
    """
    update_issue_status(issue_id, status, db_path=db_path)

    if record_id is not None:
        issues = get_issues(record_id, db_path=db_path)
        accepted_count = sum(1 for i in issues if i["status"] == "已采纳")
        rejected_count = sum(1 for i in issues if i["status"] == "已拒绝")
        update_record_stats(
            record_id, db_path=db_path, accepted_count=accepted_count, rejected_count=rejected_count
        )


def set_issue_note(issue_id: int, note: str, db_path=None) -> None:
    """更新单条问题的批注：无论采纳/拒绝/待处理，用户都可以随时写或不写。"""
    update_issue_note(issue_id, note, db_path=db_path)
