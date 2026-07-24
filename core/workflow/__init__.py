"""标准校对流程编排层。

串起 parse_document → chunk_document → proofread_document → classify_issues
全链路，并把结果落库、维护采纳/拒绝状态。不依赖 Streamlit，供 app.py 调用，
也便于脱离 Streamlit 运行时单独测试。

详细设计背景（函数签名的演变、context_snippet 计算时机、批注与状态解耦）
见 core/workflow/CLAUDE.md。
"""

from __future__ import annotations

from core.workflow.persist import persist_comparison_result, persist_result
from core.workflow.run import run_document_comparison, run_standard_proofread
from core.workflow.status import set_issue_note, set_issue_status

__all__ = [
    "run_standard_proofread",
    "persist_result",
    "run_document_comparison",
    "persist_comparison_result",
    "set_issue_status",
    "set_issue_note",
]
