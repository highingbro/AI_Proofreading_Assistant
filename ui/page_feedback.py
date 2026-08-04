"""反馈学习页：查看/重算当前生效的反馈规则，以及浏览、撤销原始拒绝记录。

规则平时由"导出Excel成功后"自动重算（`ui/actions.py::regenerate_rules_after_export`），
这里的"重新生成规则"是手动补一次的入口。
"""

from __future__ import annotations

import logging

import streamlit as st

from core import feedback, feedback_rules
from db.models import get_feedback, get_feedback_rules

logger = logging.getLogger(__name__)


def render_feedback_management():
    st.header("反馈学习")

    st.subheader("当前生效的反馈规则")
    st.caption("以下规则由历史拒绝记录经LLM语义总结得出，已注入校对提示词，AI校对时会主动规避这些模式。")
    rules = get_feedback_rules()
    if not rules:
        st.info("暂无总结出的规则。")
    else:
        for r in rules:
            st.write(f"- {r['rule_text']}")
    if st.button("重新生成规则"):
        try:
            feedback_rules.regenerate_rejection_rules()
        except Exception:
            logger.exception("反馈规则重新生成失败")
            st.error("规则重新生成失败，详见日志。")
        st.rerun()

    st.divider()
    st.subheader("原始反馈记录")
    rows = get_feedback()
    if not rows:
        st.info("暂无反馈学习记录。")
        return

    for entry in rows:
        with st.container(border=True):
            st.write(f"[{entry['issue_type']}] 原文：{entry['original_text']}")
            st.write(f"建议：{entry['suggestion']}")
            st.caption(f"AI说明：{entry['reason'] or '（无）'} · 记录时间：{entry['created_at']}")
            if st.button("撤销此条反馈", key=f"forget_feedback_{entry['feedback_id']}"):
                feedback.forget_feedback(entry["feedback_id"])
                st.rerun()
