"""原稿比对页：上传原稿+排版稿 → 段落对齐+句子级diff → 落库 → 展示 → 导出Excel。

结果展示完全复用历史记录页那一套（落库后 `get_issues` 现读 + `cards.row_to_issue_view`
+ 问题卡渲染），不照抄标准校对流的内存态展示逻辑。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

import streamlit as st

import config
from core import exporter, workflow
from core.parser import NoTextLayerError, UnsupportedFormatError
from db.models import get_issues
from ui import actions, cards

logger = logging.getLogger(__name__)


def _reset_compare_session_state():
    st.session_state["compare_record_id"] = None


def render_document_comparison():
    st.header("原稿比对")

    if "compare_record_id" not in st.session_state:
        _reset_compare_session_state()
        st.session_state["compare_file_id"] = None

    col_orig, col_fmt = st.columns(2)
    original_file = col_orig.file_uploader("上传原稿（Word）", type=["docx"], key="compare_original_uploader")
    formatted_file = col_fmt.file_uploader(
        "上传排版稿（PDF / Word）", type=["pdf", "docx"], key="compare_formatted_uploader"
    )

    # 跟标准校对流同一个理由：st.file_uploader 页面切走再切回来会变回 None，不代表
    # 用户想清空已比对结果，只有真的拿到两份新文件时才判断是否换文件/清缓存。
    if original_file is not None and formatted_file is not None:
        file_id = hashlib.md5(original_file.getvalue() + formatted_file.getvalue()).hexdigest()
        if file_id != st.session_state.get("compare_file_id"):
            _reset_compare_session_state()
            st.session_state["compare_file_id"] = file_id

    if st.session_state["compare_record_id"] is None:
        if original_file is None or formatted_file is None:
            st.info("请分别上传原稿和排版稿。")
            return
        if st.button("开始比对"):
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            original_path = config.UPLOADS_DIR / f"{timestamp}_原稿_{original_file.name}"
            formatted_path = config.UPLOADS_DIR / f"{timestamp}_排版稿_{formatted_file.name}"
            original_path.write_bytes(original_file.getvalue())
            formatted_path.write_bytes(formatted_file.getvalue())
            actions.prune_uploads_dir(config.UPLOADS_DIR)

            try:
                with st.spinner("正在比对，请稍候…"):
                    diffs, formatted_parsed = workflow.run_document_comparison(
                        str(original_path), str(formatted_path)
                    )
            except (UnsupportedFormatError, NoTextLayerError) as exc:
                logger.warning("原稿比对文档解析失败: %s", exc)
                st.error(f"文档解析失败：{exc}")
                return
            except Exception as exc:  # noqa: BLE001 兜底，避免未预期异常打崩页面
                logger.exception("原稿比对过程中发生未预期错误")
                st.error(f"比对过程中发生未预期错误：{exc}")
                return

            # 比对结果不涉及逐块LLM调用（core.comparer 是纯本地diff计算），重算成本
            # 远低于标准校对，落库失败时不做 pending/重试保存那套状态机，只兜底提示，
            # 用户重新点一次"开始比对"即可，这是有意的范围取舍。
            try:
                record_id, _ = workflow.persist_comparison_result(
                    diffs,
                    task_id=st.session_state["task_id"],
                    doc_name=f"{original_file.name} / {formatted_file.name}",
                    formatted=formatted_parsed,
                    author=st.session_state.get("author"),
                )
            except Exception as exc:  # noqa: BLE001 兜底：写库失败不能崩页面
                logger.exception("原稿比对结果落库失败")
                st.error(f"比对已完成，但保存到数据库失败：{exc}，请重试。")
                return

            st.session_state["compare_record_id"] = record_id
            st.rerun()
        return

    record_id = st.session_state["compare_record_id"]
    rows = get_issues(record_id)

    st.caption(f"共发现 {len(rows)} 处实质性内容改动（排版调整不计入，已自动过滤）。")

    # 复用历史记录页同一套"issue_status刷新+批注预填"逻辑：issue_status是全局共享的
    # session_state字典，渲染前必须用DB当前值刷新，否则会显示成陈旧/错误的状态。
    st.session_state.setdefault("issue_status", {})
    for row in rows:
        st.session_state["issue_status"][row["issue_id"]] = {"status": row["status"]}
        note_key = f"note_{row['issue_id']}"
        if note_key not in st.session_state:
            st.session_state[note_key] = row["note"] or ""

    if not rows:
        st.success("未发现排版稿与原稿之间的实质性内容改动。")
    cards.render_issue_cards([(cards.row_to_issue_view(row), row["issue_id"]) for row in rows], record_id)

    st.divider()
    if st.button("导出Excel", key=f"compare_export_{record_id}"):
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
                key=f"compare_download_{record_id}",
            )
            actions.regenerate_rules_after_export()
