"""历史记录页：当前任务下的校对记录列表 + 单条记录的完整问题卡详情。

问题卡完全复用 `ui/cards.py`，不为历史记录另写一套UI——`cards.row_to_issue_view` 负责
把数据库字典行包装成卡片期望的属性接口。
"""

from __future__ import annotations

import logging

import streamlit as st

import config
from core import exporter
from db.models import get_issues, get_records, get_tasks, update_record_task
from ui import cards

logger = logging.getLogger(__name__)


def _render_history_detail(record_id: int):
    """展示某条历史流程记录的完整问题卡列表，复用 ui/cards.py 的问题卡渲染——
    与刚校对完时同样可以采纳/拒绝/写批注/追问，所有操作直接写库，与实时流程完全一致。
    """
    issues = get_issues(record_id)

    st.session_state.setdefault("issue_status", {})
    for row in issues:
        # 每次渲染都用DB当前值刷新，保证跟采纳/拒绝按钮点击后的最新状态一致
        st.session_state["issue_status"][row["issue_id"]] = {"status": row["status"]}
        note_key = f"note_{row['issue_id']}"
        if note_key not in st.session_state:
            # 只在这个issue_id第一次出现在本次会话时预填，避免覆盖用户正在编辑但还
            # 未提交（未触发on_change）的批注内容
            st.session_state[note_key] = row["note"] or ""

    for layer in config.LAYERS:
        layer_rows = [r for r in issues if r["layer"] == layer]
        with st.expander(f"{layer}（{len(layer_rows)}条）", expanded=bool(layer_rows)):
            if not layer_rows:
                st.caption("无")
            cards.render_issue_cards(
                [(cards.row_to_issue_view(row), row["issue_id"]) for row in layer_rows], record_id
            )

    st.divider()
    if st.button("导出Excel", key=f"history_export_{record_id}"):
        try:
            export_path = exporter.export_issues_to_excel(record_id)
        except Exception as exc:  # noqa: BLE001 兜底，避免导出异常打崩页面
            logger.exception("Excel导出失败")
            st.error(f"导出失败：{exc}")
        else:
            st.success(f"已导出：{export_path}")
            st.download_button(
                "下载Excel文件",
                data=export_path.read_bytes(),
                file_name=export_path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"history_download_{record_id}",
            )


def _render_move_record_to_task(record: dict) -> None:
    """把当前选中的这条记录改归属到别的任务。

    迁移进来的历史记录全都堆在"历史归档"这一个任务下（迁移不按文档名猜任务归属），
    这里是把它们拆到真实任务里的唯一手段。改完这条记录就不再属于当前任务了，所以
    直接 rerun 让它从本页列表里消失，不做额外提示。

    已关闭的任务不出现在"移动到"选项里——已关闭在前端任何地方都不展示，不能把记录挪进
    一个前端根本看不到、也进不去的任务（见 ui/tasks.py::render_task_selection 顶部注释）。
    """
    others = [
        t for t in get_tasks()
        if t["task_id"] != record["task_id"] and t["status"] != config.TASK_STATUS_CLOSED
    ]
    if not others:
        return
    with st.expander("改归属任务"):
        labels = {f"{t['name']}（{t['status']}）": t["task_id"] for t in others}
        chosen = st.selectbox("移动到", list(labels.keys()), key=f"move_target_{record['record_id']}")
        if st.button("确认移动", key=f"move_record_{record['record_id']}"):
            update_record_task(record["record_id"], labels[chosen])
            st.rerun()


def render_history(current_task: dict):
    st.header("历史记录")
    st.caption(f"只显示当前任务「{current_task['name']}」下的校对记录。")
    records = get_records(task_id=st.session_state["task_id"])
    if not records:
        st.info("当前任务下还没有校对记录。")
        return

    # record_id 不放进 column_order 即等同隐藏——下面 selectbox 是自己从 records 里
    # 按 record_id 取值，不依赖表格是否显示这一列；其余字段名换成中文表头，时间格式化
    # 成可读形式，不再直接暴露 doc_version/task_type 等数据库原始列名。
    st.dataframe(
        records,
        hide_index=True,
        column_order=[
            "created_at", "doc_name", "author", "task_type", "mode", "total_issues",
            "count_confirmed", "count_doubtful", "count_quotation", "count_optional",
        ],
        column_config={
            "created_at": st.column_config.DatetimeColumn("时间", format="MM-DD HH:mm"),
            "doc_name": st.column_config.TextColumn("文档名", width="large"),
            "author": st.column_config.TextColumn("完成人"),
            "task_type": st.column_config.TextColumn("类型"),
            "mode": st.column_config.TextColumn("模式"),
            "total_issues": st.column_config.NumberColumn("总数"),
            "count_confirmed": st.column_config.NumberColumn(config.LAYER_CONFIRMED),
            "count_doubtful": st.column_config.NumberColumn(config.LAYER_DOUBTFUL),
            "count_quotation": st.column_config.NumberColumn(config.LAYER_QUOTATION),
            "count_optional": st.column_config.NumberColumn(config.LAYER_OPTIONAL),
        },
    )

    options = {
        f"#{r['record_id']} · {r['doc_name']} · {r['created_at']}": r["record_id"] for r in records
    }
    selected_label = st.selectbox("选择一条记录查看详情", list(options.keys()))
    selected_record_id = options[selected_label]
    record = next(r for r in records if r["record_id"] == selected_record_id)

    cards.render_doc_subtitle(record.get("doc_name"), record.get("mode"))
    if record.get("author"):
        st.caption(f"完成人：{record['author']}")
    _render_move_record_to_task(record)
    cards.render_stats(record, [])
    st.divider()
    _render_history_detail(selected_record_id)
