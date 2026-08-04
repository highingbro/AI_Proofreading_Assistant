"""标准校对页：上传 → 解析/校对/分层 → 落库 → 分层展示 → 导出Excel。

"算结果"和"存结果"是两个独立的失败域：`_execute_proofread` 拿到结果后无条件先存进
`session_state["pending_*"]`，落库单独交给 `_try_persist_pending`，落库失败不会连累
已经花掉真实API额度算出来的结果。
"""

from __future__ import annotations

import logging
from datetime import datetime

import streamlit as st

import config
from core import exporter, workflow
from core.llm_client import LLMCallError
from core.parser import NoTextLayerError, UnsupportedFormatError
from core.proofreader import LLMResponseError
from ui import actions, cards

logger = logging.getLogger(__name__)


def _reset_session_state():
    st.session_state["classified_result"] = None
    st.session_state["record_id"] = None
    st.session_state["issue_ids"] = []
    st.session_state["issue_status"] = {}
    st.session_state["mode"] = None
    # pending_* 属于上一次校对留下的"已算出结果但还没存库成功"暂存态（见
    # _try_persist_pending），换文件时这份结果已经跟不上当前上传的文件了，一并清掉。
    st.session_state["pending_result"] = None
    st.session_state["pending_parsed"] = None
    st.session_state["pending_doc_name"] = None


def _execute_proofread(uploaded_file, mode: str) -> bool:
    """实际跑一遍 解析→校对→分层，再尝试落库，把结果写进 session_state。

    从"开始校对"按钮和"重新校对本文件"按钮两处调用（后者需要 uploaded_file 还没
    因切页丢失才能直接调用，见 render_standard_proofread）。返回是否成功——失败
    时已经用 st.error 展示了原因，调用方只需要据此决定要不要 st.rerun()。

    解析/校对阶段的异常（还没花出真实API额度算出结果，或者压根没算出来）直接
    return False，没有数据可丢。一旦拿到 result/parsed（意味着已经花了真实API
    额度），无条件先存进 session_state["pending_result"/"pending_parsed"]，落库
    单独交给 _try_persist_pending 处理——即使落库失败，这份结果也不会跟着丢。
    """
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    save_path = config.UPLOADS_DIR / f"{timestamp}_{uploaded_file.name}"
    save_path.write_bytes(uploaded_file.getvalue())
    actions.prune_uploads_dir(config.UPLOADS_DIR)

    progress_bar = st.progress(0.0)
    status_text = st.empty()

    def _on_progress(current, total):
        progress_bar.progress(current / total if total else 1.0)
        status_text.text(f"已完成 {current}/{total} 块（所有块并发校对中）…")

    try:
        with st.spinner("正在校对，请稍候…"):
            result, parsed = workflow.run_standard_proofread(
                str(save_path), progress_callback=_on_progress, mode=mode
            )
    except (UnsupportedFormatError, NoTextLayerError) as exc:
        logger.warning("文档解析失败: %s", exc)
        st.error(f"文档解析失败：{exc}")
        return False
    except LLMCallError as exc:
        logger.error("LLM调用失败: %s", exc)
        st.error(f"LLM调用失败（请检查 DASHSCOPE_API_KEY 等配置）：{exc}")
        return False
    except LLMResponseError as exc:
        logger.error("LLM输出解析失败: %s", exc)
        st.error(f"LLM输出解析失败：{exc}")
        return False
    except Exception as exc:  # noqa: BLE001 兜底，避免未预期异常打崩页面
        logger.exception("校对过程中发生未预期错误")
        st.error(f"校对过程中发生未预期错误：{exc}")
        return False

    st.session_state["pending_result"] = result
    st.session_state["pending_parsed"] = parsed
    st.session_state["pending_doc_name"] = uploaded_file.name
    st.session_state["mode"] = mode
    return _try_persist_pending()


def _try_persist_pending() -> bool:
    """把 session_state 里暂存的校对结果落库；成功后清空暂存态、正式进入展示态。

    失败时保留 pending_* 不清空，只 st.error+记日志，不重新调用LLM——"落库失败
    导致已经花真实API额度算出来的结果被一起丢掉"是真实发生过的问题，这里把
    "算结果"和"存结果"两个失败域彻底分开，落库这步可以随便重试，不涉及LLM。
    """
    result = st.session_state.get("pending_result")
    parsed = st.session_state.get("pending_parsed")
    doc_name = st.session_state.get("pending_doc_name")
    mode = st.session_state.get("mode")

    try:
        record_id, issue_ids = workflow.persist_result(
            result,
            task_id=st.session_state["task_id"],
            doc_name=doc_name,
            parsed=parsed,
            mode=mode,
            author=st.session_state.get("author"),
        )
    except Exception as exc:  # noqa: BLE001 兜底：写库失败不能让已算出的结果跟着丢
        logger.exception("校对结果落库失败")
        st.error(f"校对已完成，但保存到数据库失败：{exc}。结果未丢失，可以重试保存。")
        return False

    st.session_state["classified_result"] = result
    st.session_state["record_id"] = record_id
    st.session_state["issue_ids"] = issue_ids
    st.session_state["issue_status"] = {
        issue_id: {"status": "待处理"} for issue_id in issue_ids
    }
    # 展示阶段（st.header 下的副标题）要用到文档名，pending_doc_name 落库后就清空了，
    # 单独留一份 result_doc_name 供结果页副标题显示。
    st.session_state["result_doc_name"] = doc_name
    st.session_state["pending_result"] = None
    st.session_state["pending_parsed"] = None
    st.session_state["pending_doc_name"] = None
    return True


def render_standard_proofread():
    st.header("标准校对")

    # 用标准校对专属的 classified_result 当"是否初始化过"的哨兵，不能用 issue_status——
    # 后者是历史记录页/原稿比对页共用的 key（它们会 setdefault 建起来），先逛那两个页面
    # 再回标准校对时 issue_status 已存在，会把这里的初始化跳过、导致 classified_result
    # 等键缺失后续 KeyError。classified_result 只有标准校对自己会建，用它做哨兵才准确。
    if "classified_result" not in st.session_state:
        _reset_session_state()
        st.session_state["file_id"] = None

    # 模式选择放在上传控件之前、且不依赖是否已选文件——用户应该能在上传前就定好
    # 用哪种模式，而不是必须先选文件才看得到/能调整这个选项。
    mode = st.radio("校对模式", config.PROOFREAD_MODES, horizontal=True)

    uploaded_file = st.file_uploader("上传文件（PDF / Word）", type=["pdf", "docx"])

    # 注意：st.file_uploader 在页面切走再切回来后，浏览器出于安全限制无法恢复
    # 之前选中的文件，uploaded_file 会变回 None——这不代表用户想清空已校对结果，
    # 所以只有在 uploaded_file 真的拿到新文件时才判断是否换文件/清缓存；
    # uploaded_file 为 None 时直接往下走，看有没有已缓存的结果可以展示。
    if uploaded_file is not None:
        file_id = actions.file_id(uploaded_file)
        if file_id != st.session_state.get("file_id"):
            _reset_session_state()
            st.session_state["file_id"] = file_id

    if st.session_state["classified_result"] is None:
        if st.session_state.get("pending_result") is not None:
            # 校对已经跑完（花了真实API额度），但上次尝试保存到数据库失败——结果
            # 还在内存里，不需要重新校对，只需要重试保存这一步。
            st.warning(
                f"「{st.session_state.get('pending_doc_name')}」已完成校对，"
                "但保存到数据库失败，结果仍保留在内存中，未丢失。"
            )
            if st.button("重试保存"):
                if _try_persist_pending():
                    st.rerun()
            return
        if uploaded_file is None:
            return
        if st.button("开始校对"):
            if _execute_proofread(uploaded_file, mode):
                st.rerun()
        return

    result = st.session_state["classified_result"]
    record_id = st.session_state["record_id"]
    issue_ids = st.session_state["issue_ids"]

    cards.render_doc_subtitle(
        st.session_state.get("result_doc_name"), st.session_state.get("mode")
    )

    if st.button("重新校对本文件", key="rerun_proofread"):
        if uploaded_file is not None:
            # 文件对象还在（没离开过页面），直接原地重新执行一遍，不需要用户再点一次
            # "开始校对"、更不需要重新上传。
            if _execute_proofread(uploaded_file, mode):
                st.rerun()
        else:
            # uploaded_file 因切页丢失（浏览器安全限制，见上方说明），没有文件字节可用，
            # 没法直接重跑，只能清空缓存结果、退回等待重新上传的状态。
            st.warning("页面切换后文件已从浏览器丢失，请重新上传该文件后点击「开始校对」。")
            _reset_session_state()
            st.rerun()

    cards.render_stats(result.stats, result.warnings)

    st.divider()
    for layer in config.LAYERS:
        layer_issues = [
            (issue, issue_id)
            for issue, issue_id in zip(result.issues, issue_ids)
            if issue.layer == layer
        ]
        with st.expander(f"{layer}（{len(layer_issues)}条）", expanded=bool(layer_issues)):
            if not layer_issues:
                st.caption("无")
            cards.render_issue_cards(layer_issues, record_id)

    st.divider()
    if st.button("导出Excel"):
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
            )
            actions.regenerate_rules_after_export()
