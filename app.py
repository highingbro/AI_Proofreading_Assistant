"""Streamlit 入口：署名 → 任务闸门 → 侧边栏 → 四个功能页的路由。

渲染逻辑全在 ui/ 包里，本文件只保留有严格先后顺序的模块级脚本段。UI 设计决策
（色彩语义、卡片布局、任务闸门、session_state 生命周期、两处状态同步的坑等）
见 ui/CLAUDE.md。
"""

import logging

import streamlit as st

import config
from db.database import init_db
from db.models import get_task
from ui import page_compare, page_feedback, page_history, page_standard, tasks, theme

# 配置的是 root logger，ui/ 各模块自建的子 logger 一并落到同一个文件。
logging.basicConfig(
    filename=str(config.LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    encoding="utf-8",
)

st.set_page_config(page_title="出版校对AI助手", layout="wide")

theme.inject_sidebar_brand()
theme.inject_card_styles()

init_db()


tasks.render_author_picker()

# 任务闸门：session_state 里没有一个仍然存在的当前任务时，只渲染任务选择界面。
# get_task 的 None 兜底覆盖"库被换掉/任务被删掉"导致 task_id 悬空的情况。
current_task = (
    get_task(st.session_state["task_id"]) if st.session_state.get("task_id") else None
)
if current_task is None:
    st.session_state.pop("task_id", None)
    tasks.render_task_selection()
    st.stop()

st.sidebar.markdown(f"**当前任务**　{current_task['name']}")
st.sidebar.caption(f"状态：{current_task['status']}")
if st.sidebar.button("切换任务"):
    tasks.switch_task()

st.sidebar.divider()
page = st.sidebar.radio("功能入口", ("标准校对", "原稿比对", "历史记录", "反馈学习"))


if page == "标准校对":
    page_standard.render_standard_proofread()
elif page == "原稿比对":
    page_compare.render_document_comparison()
elif page == "历史记录":
    page_history.render_history(current_task)
else:
    page_feedback.render_feedback_management()
