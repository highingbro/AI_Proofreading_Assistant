"""问题卡片与统计指标的渲染，标准校对页/历史记录页/原稿比对页三处共用同一份。

复用的关键是 `row_to_issue_view`：实时校对流程手里是内存中的 `ClassifiedIssue`（属性
访问），从数据库读回来的是字典行，用它抹平接口差异，卡片UI因此只有一套。
"""

from __future__ import annotations

import html
import logging
import types

import streamlit as st

import config
from core import feedback, followup, workflow
from core.llm_client import LLMCallError
from ui import theme

logger = logging.getLogger(__name__)


def render_doc_subtitle(doc_name: str | None, mode: str | None) -> None:
    """在 st.header 之下渲染"文档名 · 模式"副标题（目标设计里页面标题下那行灰字）。
    doc_name 缺失时只显示模式，两者都缺时不渲染。
    """
    parts = [p for p in (doc_name, f"{mode}模式" if mode else None) if p]
    if parts:
        st.caption(" · ".join(parts))


def render_stats(stats: dict, warnings: list):
    """渲染统计总结句 + 六个对齐的指标数字。stats 取值只用 .get()，兼容 ClassifiedResult.stats
    和 db.models.get_records() 返回的原始字典（字段名本就一一对应，历史记录页直接传records行）。

    文档名/校对模式的副标题由调用方在 st.header 之下自行渲染（那里才拿得到 doc_name），
    不在本函数里。
    """
    total_issues = stats.get("total_issues", 0)
    confirmed = stats.get("count_confirmed", 0)
    doubtful = stats.get("count_doubtful", 0)
    quotation = stats.get("count_quotation", 0)
    optional = stats.get("count_optional", 0)
    high = stats.get("high_priority_count", 0)

    # 0个问题 + 存在警告，很可能是"部分/全部chunk校对失败"而不是"文档真的没问题"——
    # 这两种情况在"总问题数"这个最显眼的指标上长得一模一样，不能只让用户自己点开
    # 下面默认折叠的提示框才发现，必须在结果为0时主动提醒来看警告。
    if total_issues == 0 and warnings:
        st.error(
            f"本次结果为0条问题，但同时有{len(warnings)}条处理失败/异常提示——"
            "这很可能不是「文档没有问题」，而是部分或全部内容没有被真正校对到，"
            "请务必查看下方「提示信息」再下结论。"
        )

    # 先给一句自然语言总结（"助手会总结，工具只会统计"），再列指标数字。
    st.markdown(
        f"本次共发现 **{total_issues}** 处："
        f"{config.LAYER_CONFIRMED} {confirmed}、{config.LAYER_DOUBTFUL} {doubtful}、"
        f"{config.LAYER_QUOTATION} {quotation}、{config.LAYER_OPTIONAL} {optional}"
        f"（其中高优先级 {high}）。"
    )

    # 六个指标用同一个HTML flex行渲染，保证标签/数字在各列之间基线对齐（之前 st.metric
    # 和手写markdown混用导致高低不齐）；四个层级数字用 theme.LAYER_COLOR 语义色，
    # 总数/高优先级用中性深色。
    cells = (
        ("总问题数", total_issues, "#1A1A1A"),
        (config.LAYER_CONFIRMED, confirmed, theme.LAYER_COLOR[config.LAYER_CONFIRMED]),
        (config.LAYER_DOUBTFUL, doubtful, theme.LAYER_COLOR[config.LAYER_DOUBTFUL]),
        (config.LAYER_QUOTATION, quotation, theme.LAYER_COLOR[config.LAYER_QUOTATION]),
        (config.LAYER_OPTIONAL, optional, theme.LAYER_COLOR[config.LAYER_OPTIONAL]),
        ("高优先级", high, "#1A1A1A"),
    )
    cells_html = "".join(
        "<div style='flex:1;min-width:90px'>"
        f"<div style='font-size:0.8rem;color:#666;margin-bottom:2px'>{html.escape(label)}</div>"
        f"<div style='font-size:2rem;font-weight:600;line-height:1.15;color:{color}'>{value}</div>"
        "</div>"
        for label, value, color in cells
    )
    st.markdown(
        f"<div style='display:flex;gap:16px;flex-wrap:wrap;margin:6px 0 4px'>{cells_html}</div>",
        unsafe_allow_html=True,
    )

    if warnings:
        with st.expander(f"提示信息（{len(warnings)}条）", expanded=(total_issues == 0)):
            for w in warnings:
                st.warning(w)


def _render_issue_card(issue, issue_id, record_id):
    status_info = st.session_state["issue_status"].setdefault(issue_id, {"status": "待处理"})
    status = status_info["status"]

    # 容器 key 决定卡片左侧色条颜色（见 ui/theme.py::inject_card_styles，不给整卡上底色）：
    # 待处理态按层级色，已采纳=绿、已拒绝=红。用固定前缀区分终态，rerun 后 key 变化、
    # 色条随之切换，不需要额外状态管理代码。
    if status == "已采纳":
        card_key = f"issue_card_accepted_{issue_id}"
    elif status == "已拒绝":
        card_key = f"issue_card_rejected_{issue_id}"
    else:
        card_key = f"issue_card_{theme.LAYER_SLUG.get(issue.layer, theme.OTHER_LAYER_SLUG)}_{issue_id}"

    layer_color = theme.LAYER_COLOR.get(issue.layer, theme.OTHER_LAYER_COLOR)
    # 标题：层级名+定位用层级色，优先级用灰色；终态再追加一个绿/红状态标签。
    header = (
        f"<span style='color:{layer_color};font-weight:600'>"
        f"{html.escape(issue.layer)} · {html.escape(issue.page_location or '未定位')}</span>"
        f"<span style='color:#999;font-weight:400'> · {html.escape(issue.priority)}</span>"
    )
    if status in theme.STATUS_COLOR:
        header += (
            f"<span style='color:{theme.STATUS_COLOR[status]};font-weight:600'> · {status}</span>"
        )

    with st.container(border=True, key=card_key, gap="xxsmall"):
        st.markdown(header, unsafe_allow_html=True)
        # 干净地分两块展示原文和建议（不做字符级diff：suggestion 是自由文本说明，不是
        # 平行的"改后文本"，硬做diff只会得到乱码）。用小灰标签区分两块，正文都用黑字，
        # 一眼能看清哪句是原文、哪句是建议。原文本身是被标记的一小段原句，长度稳定，
        # 不预留高度；建议是自由文本说明，长短差异大，才是同一行两张卡参差不齐的来源，
        # 只在建议上预留两行高度（min-height+line-height，短文本也占满这份高度）——
        # 这个预留不参与"压卡片高度"，压缩改从别处拿（卡片padding、容器gap、批注行合并
        # 到按钮行，见下方与 ui/theme.py::inject_card_styles）；建议字号略调小，减少
        # 超出两行的概率。容器整体用 gap="xxsmall" 压缩，但那个间距对"建议"这段正文和
        # 下一个元素（归层依据expander，或没有该expander时的采纳/拒绝按钮行）来说太挤、
        # 看着像糊在一起，所以在建议块自己身上单独补一个 margin-bottom，不改动其他地方
        # 的间距（不拉高header-content、按钮行-追问expander之间已经压紧的空隙）。
        st.markdown(
            f"<div style='margin-top:1px'><span style='color:#999;font-size:0.78rem'>原文</span>"
            f"<div style='color:#1A1A1A'>{html.escape(issue.original_text)}</div></div>"
            f"<div style='margin-top:3px;margin-bottom:10px'>"
            f"<span style='color:#999;font-size:0.78rem'>建议</span>"
            f"<div style='color:#1A1A1A;font-size:0.9rem;line-height:1.4;min-height:2.8em'>"
            f"{html.escape(issue.suggestion)}</div></div>",
            unsafe_allow_html=True,
        )

        # 存疑类/引文类的判断依赖置信度/引用识别，需要人工复核依据；错误类/风格类判断
        # 相对直接，不展示这个expander以免信息过载（见 theme.LAYERS_WITH_NOTES 定义处说明）。
        if issue.layer in theme.LAYERS_WITH_NOTES:
            with st.expander("归层依据"):
                st.caption("；".join(issue.layer_notes))

        def _save_note():
            workflow.set_issue_note(issue_id, st.session_state.get(f"note_{issue_id}", ""))

        # 批注输入框跟采纳/拒绝（或撤销）按钮挤在同一行的剩余空间里，不再单独占一整行——
        # 是压缩卡片高度的一部分，其余是上面 min-height 收紧和 ui/theme.py::inject_card_styles
        # 里的 padding 收紧。label_visibility="collapsed" 省掉的"批注"文字标签行用占位符
        # 补上，紧挨着按钮不影响辨识。
        if status == "待处理":
            col_accept, col_reject, col_note = st.columns([1, 1, 4])
            if col_accept.button("采纳", key=f"accept_{issue_id}"):
                workflow.set_issue_status(issue_id, "已采纳", record_id=record_id)
                status_info["status"] = "已采纳"
                st.rerun()

            if col_reject.button("拒绝", key=f"reject_{issue_id}"):
                workflow.set_issue_status(issue_id, "已拒绝", record_id=record_id)
                feedback.record_rejection(issue, issue_id, record_id)
                # 只快速记录这条拒绝，不在这里同步跑规则总结——那是一次几秒的LLM调用，
                # 连续拒绝会次次触发、又慢又费额度。规则重算改到"导出Excel成功后"
                # （ui/actions.py::regenerate_rules_after_export，一轮审校的收尾动作）
                # 和反馈页手动按钮触发。
                status_info["status"] = "已拒绝"
                st.rerun()
        else:
            # 卡片底色已经表达了"已采纳"/"已拒绝"这个终态，不再需要"— 待处理"这类文字；
            # 撤销直接把状态改回待处理，复用通用的 set_issue_status，不新增函数。撤销
            # "已拒绝"不撤销已写入的 feedback 表记录——反馈学习是独立的历史留痕。
            col_undo, col_note = st.columns([1, 5])
            if col_undo.button("撤销", key=f"undo_{issue_id}"):
                workflow.set_issue_status(issue_id, "待处理", record_id=record_id)
                status_info["status"] = "待处理"
                st.rerun()

        col_note.text_input(
            "批注",
            key=f"note_{issue_id}",
            on_change=_save_note,
            placeholder="批注（可选）",
            label_visibility="collapsed",
        )

        with st.expander("追问"):
            for turn in followup.get_followup_history(issue_id):
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
                            followup.answer_followup(issue_id, question)
                    except LLMCallError as exc:
                        logger.error("追问失败: %s", exc)
                        st.error(f"追问失败：{exc}")
                    else:
                        st.rerun()


def render_issue_cards(items, record_id, n_cols: int = 2) -> None:
    """两条一行的网格布局渲染问题卡列表：一条卡打满整行的话，问题一多列表纵向就很长。

    items 是 (issue, issue_id) 二元组列表；和任务选择页任务卡的两列网格是同一个模式
    （见 ui/CLAUDE.md）。**列数不要加到三**：单卡变窄后原文/建议更容易换行，同一行内
    卡片高度参差不齐反而更明显。
    """
    for i in range(0, len(items), n_cols):
        cols = st.columns(n_cols)
        for col, (issue, issue_id) in zip(cols, items[i : i + n_cols]):
            with col:
                _render_issue_card(issue, issue_id, record_id)


def row_to_issue_view(row: dict):
    """把 db.models.get_issues() 返回的原始字典行，包装成 _render_issue_card 期望的
    属性接口（.priority/.page_location/...），使问题卡渲染逻辑在实时校对流程、历史
    记录页、原稿比对结果三处之间原样复用，不需要各自另写一套卡片UI。

    layer_notes 是 ClassifiedIssue 独有字段（归层依据），只在标准校对当次运行时存在
    于内存里，issues 表没有对应列——从数据库现
    读出来渲染的场景（历史记录页、原稿比对结果，两者都是先落库再用 get_issues 读回
    来展示）都没有这份数据可展示，用一句说明文字占位，不是缺陷、也不假装有数据。
    """
    return types.SimpleNamespace(
        priority=row["priority"],
        page_location=row["page_location"],
        issue_type=row["issue_type"],
        original_text=row["original_text"],
        suggestion=row["suggestion"],
        layer=row["layer"],
        layer_notes=["（该字段仅在标准校对当次运行时于内存中可用，未持久化，此处无法展示）"],
    )
