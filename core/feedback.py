"""用户反馈记录（薄封装）。

同一类被人工判定"判错了"的问题（点击"拒绝"）会在后续校对（同一文档的其他位置、或
后续上传的同类文档）里反复出现——本模块只负责"记一条拒绝"（`record_rejection`）和
"删一条拒绝"（`forget_feedback`）这两件事，是 `db/models.py` 的薄封装，供 `app.py`
直接调用。把历史拒绝记录汇总成规则、注入校对提示词的语义总结逻辑在
`core/feedback_rules.py`，不在本模块——本模块不做任何相似度匹配或分类判断。
"""

from __future__ import annotations

from db.models import add_feedback, delete_feedback

__all__ = ["record_rejection", "forget_feedback"]


def record_rejection(issue, issue_id: int, record_id: int, db_path=None) -> None:
    """issue被人工拒绝时调用，记录一条反馈快照。

    suggestion 在实时校对流程（ClassifiedIssue）和历史记录页（_row_to_issue_view 包装的
    SimpleNamespace）两种 issue 对象上都存在，不需要兜底；reason 只在实时流程的
    ClassifiedIssue 上存在，用 getattr 兜底成空字符串——这里只影响管理页的展示信息。
    """
    add_feedback(
        issue_type=issue.issue_type,
        original_text=issue.original_text,
        suggestion=issue.suggestion,
        reason=getattr(issue, "reason", "") or "",
        source_issue_id=issue_id,
        source_record_id=record_id,
        db_path=db_path,
    )


def forget_feedback(feedback_id: int, db_path=None) -> None:
    """管理页"撤销此条反馈"按钮调用，删除一条历史反馈记录。"""
    delete_feedback(feedback_id, db_path=db_path)
