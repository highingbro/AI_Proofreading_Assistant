"""Streamlit 入口（阶段6标准校对流+阶段8Excel导出+阶段7追问+阶段10历史记录详情页；原稿比对仍是占位）。

## 标准校对分支（阶段6）要点

Streamlit 每次交互都会重跑整个脚本，全部靠 st.session_state（file_id/
classified_result/record_id/issue_ids/issue_status）管住生命周期：换文件
（按文件内容md5哈希判定）才清缓存重新校对；采纳/拒绝按钮触发的 rerun 只读
缓存、不重跑 workflow.run_standard_proofread。没有用 st.cache_*——校对流程
有副作用（写库、耗真实API额度），语义上不适合按输入哈希缓存的机制。

上传的文件先落盘到 config.UPLOADS_DIR（时间戳前缀避免覆盖）再交给
parse_document（该函数吃路径不吃文件对象）。问题卡按钮 key 用
f"accept_{issue_id}"/f"reject_{issue_id}"，全局唯一。异常兜底：
UnsupportedFormatError/NoTextLayerError/LLMCallError/LLMResponseError 分别
给可读提示，兜底 except Exception 防止未预期异常崩页面；出错时不写库、不
缓存结果，允许重试。

st.file_uploader 在页面切走再切回来后，浏览器出于安全限制无法恢复之前选中
的文件，uploaded_file 会变回 None——这不代表用户想清空已校对结果，所以只
有在 uploaded_file 真的拿到新文件时才判断是否换文件/清缓存；uploaded_file
为 None 时直接往下走，看有没有已缓存的结果可以展示。

## 追问区域（阶段7）要点

_render_issue_card 在采纳/拒绝按钮下方加了"追问"expander（历史用
st.chat_message 渲染，新问题用 text_input+按钮提交，answer_followup 抛出
的 LLMCallError 用 st.error 兜住）。追问历史全程从数据库现读，不进
session_state，和阶段8导出"数据一律从库读"是同一个原则。

## 导出Excel按钮（阶段8）要点

点击调 core.exporter.export_issues_to_excel(record_id, db_path=None)，异常
用 st.error 兜住；成功后 st.success 提示路径 + st.download_button 提供下载
（mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"）。

## 批注输入框（"批注与状态解耦"补丁）要点

紧贴采纳/拒绝按钮下方独立一行的"批注（可选，采纳/拒绝/待处理都可以写）"，
用 st.text_input(..., on_change=_save_note) 实现——不需要额外的"保存"按钮，
失焦/回车即通过 on_change 回调写库，批注值本身就靠该 widget 自身 key 对应
的 st.session_state 在 rerun 间保持，不需要在 issue_status 缓存字典里额外
镜像一份。详见 core/workflow/CLAUDE.md"补丁：批注与状态解耦"一节。

## 历史记录详情页（阶段10）要点

真实使用中发现"历史记录"页原本只是阶段1遗留的骨架（st.dataframe(get_records())
直接把 records 表原样甩出来，只能看统计数字），用户要求补上"点进某条历史
记录、像刚校对完那样看到完整问题卡列表"。

核心设计：完全复用 _render_issue_card，不为历史记录页另写一套卡片UI。这个
函数从阶段6起一直按"属性访问"（issue.priority/issue.page_location/…）编写，
参数在实时校对流程里是内存中的 ClassifiedIssue；历史记录页的数据来源是
get_issues(record_id) 返回的字典（DB行），两者接口不兼容。没有为此重写
_render_issue_card 或改成字典访问（改了会牵动阶段6/7/8所有既有调用点和测
试），而是新增 _row_to_issue_view(row: dict)，用 types.SimpleNamespace 把
DB字典行包成同样支持属性访问的对象，实时流程和历史记录页因此共用同一份卡
片渲染逻辑，包括采纳/拒绝按钮、批注输入框、追问expander——全部原样可用，
因为这些交互本来就是直接写库的，不依赖是从哪个页面触发的。

layer_notes（归层依据）历史记录页展示不出来，是数据本身没有，不是遗漏：
issues 表从阶段5/8设计起就没有 layer_notes 列（ClassifiedIssue.layer_notes
只在校对当次运行时存在于内存里，persist_result 从未把它落库）。
_row_to_issue_view 对这个字段填的是一句说明文字，不是留空更不是编造假数据。

两处状态同步的坑，处理方式：

1. _render_issue_card 用
   st.session_state["issue_status"].setdefault(issue_id, {"status": "待处理"})
   取显示用的状态——这个 setdefault 是给实时流程设计的（issue刚生成时确实
   是"待处理"）。如果历史记录页不做任何处理直接复用，会把DB里明明"已采纳"
   的历史issue，在还没被点击过的这个新会话里错误显示成"待处理"。修复：
   _render_history_detail 在渲染问题卡之前，先用 get_issues 查到的DB当前值
   无条件覆写 st.session_state["issue_status"][issue_id]（不是 setdefault，
   就是直接赋值），每次进页面/切换记录/点完按钮触发 rerun 后都会重新执行
   这一步，保证展示的状态永远是DB真实值。
2. 批注输入框 st.text_input(key=f"note_{issue_id}", ...) 同理：Streamlit
   组件的初始值来自 st.session_state[key]，历史issue的批注只存在DB里，这
   个会话从没设置过对应的 note_{issue_id} key，不预填就会显示空白，看起来
   像"批注丢了"。修复：_render_history_detail 在第一次遇到某个 issue_id 时
   （if note_key not in st.session_state）用DB的 note 值预填——用条件预填
   而不是像状态那样无条件覆写，是为了不打断用户正在输入但还没触发
   on_change（失焦/回车）的编辑内容。

_render_stats 签名从只接受 ClassifiedResult 改成接受
(stats: dict, warnings: list, mode: str | None) 三个原始值——
db.models.get_records() 返回的字典字段名本就和 ClassifiedResult.stats 一一
对应，直接传历史record字典即可，不需要额外转换；warnings 历史记录页传空
列表（records 表没存解析/分块警告），mode 直接传 record["mode"]。这是唯一
一处修改了既有函数签名的地方，实时流程调用点同步改为
_render_stats(result.stats, result.warnings, st.session_state.get("mode"))，
行为不变。

导出Excel按钮在历史记录页复用 core.exporter.export_issues_to_excel，与阶段
8导出按钮实现一致；key= 加上 record_id 后缀
（history_export_{record_id}/history_download_{record_id}）避免和标准校对
页的同名按钮/未来可能的多记录场景冲突。

## 未覆盖范围

框架文档把"异常处理（解析失败、API超时重试）"归在阶段10，但这部分早已随
阶段4（LLM调用指数退避重试，见 core/llm_client.py 模块docstring）、阶段6
（本文件对 UnsupportedFormatError/NoTextLayerError/LLMCallError/
LLMResponseError/兜底 Exception 的分类捕获）分散实现，未单独验收，不是没做。
"""

import hashlib
import types
from datetime import datetime

import streamlit as st

import config
from core import exporter, followup, workflow
from core.llm_client import LLMCallError
from core.parser import NoTextLayerError, UnsupportedFormatError
from core.proofreader import LLMResponseError
from db.database import init_db
from db.models import get_issues, get_records

init_db()

st.set_page_config(page_title="出版校对AI助手", layout="wide")
st.title("出版校对AI助手")

page = st.sidebar.radio("功能入口", ("标准校对", "原稿比对", "历史记录"))


def _file_id(uploaded_file) -> str:
    return hashlib.md5(uploaded_file.getvalue()).hexdigest()


def _reset_session_state():
    st.session_state["classified_result"] = None
    st.session_state["record_id"] = None
    st.session_state["issue_ids"] = []
    st.session_state["issue_status"] = {}
    st.session_state["mode"] = None


def _render_stats(stats: dict, warnings: list, mode: str | None):
    """渲染统计指标卡片。stats 取值只用 .get()，兼容 ClassifiedResult.stats 和
    db.models.get_records() 返回的原始字典（字段名本就一一对应，历史记录页直接传records行）。
    """
    st.caption(f"校对模式：{mode or '未知'}")
    cols = st.columns(6)
    cols[0].metric("总问题数", stats.get("total_issues", 0))
    cols[1].metric(config.LAYER_CONFIRMED, stats.get("count_confirmed", 0))
    cols[2].metric(config.LAYER_DOUBTFUL, stats.get("count_doubtful", 0))
    cols[3].metric(config.LAYER_QUOTATION, stats.get("count_quotation", 0))
    cols[4].metric(config.LAYER_OPTIONAL, stats.get("count_optional", 0))
    cols[5].metric("高优先级", stats.get("high_priority_count", 0))

    if warnings:
        with st.expander(f"⚠ 提示信息（{len(warnings)}条）"):
            for w in warnings:
                st.warning(w)


def _render_issue_card(issue, issue_id, record_id):
    status_info = st.session_state["issue_status"].setdefault(issue_id, {"status": "待处理"})
    is_high = issue.priority == config.PRIORITY_HIGH

    border_color = "🔴" if is_high else "🔹"
    header = f"{border_color} [{issue.priority}] [{issue.page_location or '未定位'}] {issue.issue_type} — {status_info['status']}"

    with st.container(border=True):
        st.markdown(f"**{header}**")
        st.write(f"原文：{issue.original_text}")
        st.write(f"建议：{issue.suggestion}")
        with st.expander("归层依据"):
            st.caption("；".join(issue.layer_notes))

        col_accept, col_reject = st.columns(2)
        if col_accept.button("采纳", key=f"accept_{issue_id}"):
            workflow.set_issue_status(issue_id, "已采纳", record_id=record_id, db_path=None)
            status_info["status"] = "已采纳"
            st.rerun()

        if col_reject.button("拒绝", key=f"reject_{issue_id}"):
            workflow.set_issue_status(issue_id, "已拒绝", record_id=record_id, db_path=None)
            status_info["status"] = "已拒绝"
            st.rerun()

        def _save_note():
            workflow.set_issue_note(issue_id, st.session_state.get(f"note_{issue_id}", ""), db_path=None)

        st.text_input("批注（可选，采纳/拒绝/待处理都可以写）", key=f"note_{issue_id}", on_change=_save_note)

        with st.expander("追问"):
            for turn in followup.get_followup_history(issue_id, db_path=None):
                with st.chat_message("user"):
                    st.write(turn["question"])
                with st.chat_message("assistant"):
                    st.write(turn["answer"])

            question = st.text_input(
                "输入追问", key=f"followup_input_{issue_id}", label_visibility="collapsed"
            )
            if st.button("发送", key=f"followup_submit_{issue_id}"):
                if question.strip():
                    try:
                        with st.spinner("正在思考…"):
                            followup.answer_followup(issue_id, question, db_path=None)
                    except LLMCallError as exc:
                        st.error(f"追问失败：{exc}")
                    else:
                        st.rerun()


def _render_standard_proofread():
    st.header("标准校对")

    if "issue_status" not in st.session_state:
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
        file_id = _file_id(uploaded_file)
        if file_id != st.session_state.get("file_id"):
            _reset_session_state()
            st.session_state["file_id"] = file_id

    if st.session_state["classified_result"] is None:
        if uploaded_file is None:
            return
        if st.button("开始校对"):
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            save_path = config.UPLOADS_DIR / f"{timestamp}_{uploaded_file.name}"
            save_path.write_bytes(uploaded_file.getvalue())

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
                st.error(f"文档解析失败：{exc}")
                return
            except LLMCallError as exc:
                st.error(f"LLM调用失败（请检查 DASHSCOPE_API_KEY 等配置）：{exc}")
                return
            except LLMResponseError as exc:
                st.error(f"LLM输出解析失败：{exc}")
                return
            except Exception as exc:  # noqa: BLE001 兜底，避免未预期异常打崩页面
                st.error(f"校对过程中发生未预期错误：{exc}")
                return

            record_id, issue_ids = workflow.persist_result(
                result, doc_name=uploaded_file.name, parsed=parsed, mode=mode, db_path=None
            )
            st.session_state["classified_result"] = result
            st.session_state["record_id"] = record_id
            st.session_state["issue_ids"] = issue_ids
            st.session_state["issue_status"] = {
                issue_id: {"status": "待处理"} for issue_id in issue_ids
            }
            st.session_state["mode"] = mode
            st.rerun()
        return

    result = st.session_state["classified_result"]
    record_id = st.session_state["record_id"]
    issue_ids = st.session_state["issue_ids"]

    _render_stats(result.stats, result.warnings, st.session_state.get("mode"))

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
            for issue, issue_id in layer_issues:
                _render_issue_card(issue, issue_id, record_id)

    st.divider()
    if st.button("导出Excel"):
        try:
            export_path = exporter.export_issues_to_excel(record_id, db_path=None)
        except Exception as exc:  # noqa: BLE001 兜底，避免导出异常打崩页面
            st.error(f"导出失败：{exc}")
        else:
            st.success(f"已导出：{export_path}")
            st.download_button(
                "下载Excel文件",
                data=export_path.read_bytes(),
                file_name=export_path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )


def _row_to_issue_view(row: dict):
    """把 db.models.get_issues() 返回的原始字典行，包装成 _render_issue_card 期望的
    属性接口（.priority/.page_location/...），使问题卡渲染逻辑在实时校对流程和历史
    记录页之间原样复用，不需要为历史记录页另写一套卡片UI。

    layer_notes 是 ClassifiedIssue 独有字段（归层依据），只在校对当次运行时存在于
    内存里，从未持久化进 issues 表（阶段5/8设计如此，见 CLAUDE.md）——历史记录页
    没有这份数据可展示，用一句说明文字占位，不是缺陷、也不假装有数据。
    """
    return types.SimpleNamespace(
        priority=row["priority"],
        page_location=row["page_location"],
        issue_type=row["issue_type"],
        original_text=row["original_text"],
        suggestion=row["suggestion"],
        layer_notes=["（历史记录未保存归层依据的详细说明，该字段仅在校对当次运行时于内存中可用）"],
    )


def _render_history_detail(record_id: int):
    """展示某条历史流程记录的完整问题卡列表，复用 _render_issue_card——
    与刚校对完时同样可以采纳/拒绝/写批注/追问，所有操作直接写库，与实时流程完全一致。
    """
    issues = get_issues(record_id, db_path=None)

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
            for row in layer_rows:
                _render_issue_card(_row_to_issue_view(row), row["issue_id"], record_id)

    st.divider()
    if st.button("导出Excel", key=f"history_export_{record_id}"):
        try:
            export_path = exporter.export_issues_to_excel(record_id, db_path=None)
        except Exception as exc:  # noqa: BLE001 兜底，避免导出异常打崩页面
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


def _render_history():
    st.header("历史记录")
    records = get_records()
    if not records:
        st.info("暂无历史校对记录。")
        return

    st.dataframe(records, hide_index=True)

    options = {
        f"#{r['record_id']} · {r['doc_name']} · {r['created_at']}": r["record_id"] for r in records
    }
    selected_label = st.selectbox("选择一条记录查看详情", list(options.keys()))
    selected_record_id = options[selected_label]
    record = next(r for r in records if r["record_id"] == selected_record_id)

    _render_stats(record, [], record.get("mode"))
    st.divider()
    _render_history_detail(selected_record_id)


if page == "标准校对":
    _render_standard_proofread()
elif page == "原稿比对":
    st.header("原稿比对")
    st.info("该功能将在后续阶段实现。")
else:
    _render_history()
