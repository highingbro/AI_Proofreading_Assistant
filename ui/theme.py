"""四层分类的语义色板，以及页面级一次性注入的品牌标识与卡片样式。

色值集中在这里是因为同一个层级要在多处保持同色：指标区数字（`ui/cards.py::render_stats`）、
问题卡片的左侧色条与标题（`ui/cards.py::_render_issue_card`）。
"""

from __future__ import annotations

import streamlit as st

import config

LAYER_SLUG = {
    config.LAYER_CONFIRMED: "confirmed",
    config.LAYER_DOUBTFUL: "doubtful",
    config.LAYER_QUOTATION: "quotation",
    config.LAYER_OPTIONAL: "optional",
}
# 四层语义色，贯穿指标区数字、问题卡片的层级色条与标题——同一层级在哪都用同一个色。
LAYER_COLOR = {
    config.LAYER_CONFIRMED: "#D66E45",
    config.LAYER_DOUBTFUL: "#C5C81F",
    config.LAYER_QUOTATION: "#3B7DD8",
    config.LAYER_OPTIONAL: "#7A7288FF",
}
# 采纳=绿、拒绝=红，只用在卡片左侧色条与状态标签上（不给整卡上底色）。
STATUS_COLOR = {"已采纳": "#2E8B57", "已拒绝": "#D64570"}
# 库里可能存着四层之外的 layer 取值（早期原稿比对记录用的是"实质性改动"这套独立取值），
# 不在 LAYER_SLUG/LAYER_COLOR 里——用这个中性灰兜底，避免翻旧记录时 KeyError。
OTHER_LAYER_SLUG = "other"
OTHER_LAYER_COLOR = "#8a8f98"
# 存疑类/引文类的判断依赖置信度/引用识别，需要人工复核依据；错误类/风格类判断相对
# 直接，卡片不展示"归层依据"以免信息过载。
LAYERS_WITH_NOTES = (config.LAYER_DOUBTFUL, config.LAYER_QUOTATION)


def inject_card_styles() -> None:
    """一次性注入问题卡片的层级左色条样式。

    不给整卡上底色（用户明确要求"背景不上色"），只在卡片左侧加一条4px色条表达层级/
    状态：待处理态按层级色，已采纳=绿、已拒绝=红。靠 st.container(border=True, key=...)
    自动生成的稳定 st-key-<key> CSS class（Streamlit 1.32+）做属性选择器，一条规则覆盖
    该层级/状态的所有卡片，不需要为每张卡片单独出样式。终态 key 用固定前缀（不含层级
    slug），因此"层级×终态"不必各写一条规则。
    """
    # padding 从 Streamlit 默认的 1rem 收紧到 0.5rem/0.8rem，是压缩卡片高度的一部分
    # （另一部分是 ui/cards.py::_render_issue_card 内部的 min-height/gap 收紧），只用于
    # 问题卡，不影响页面其他 st.container(border=True)（选择器限定在 st-key-issue_card_ 前缀）。
    bar = "border-left: 4px solid {color}; border-radius: 2px; padding: 0.5rem 0.8rem;"
    rules = "\n".join(
        f'div[class*="st-key-issue_card_{slug}_"] {{ {bar.format(color=color)} }}'
        for slug, color in (
            (LAYER_SLUG[config.LAYER_CONFIRMED], LAYER_COLOR[config.LAYER_CONFIRMED]),
            (LAYER_SLUG[config.LAYER_DOUBTFUL], LAYER_COLOR[config.LAYER_DOUBTFUL]),
            (LAYER_SLUG[config.LAYER_QUOTATION], LAYER_COLOR[config.LAYER_QUOTATION]),
            (LAYER_SLUG[config.LAYER_OPTIONAL], LAYER_COLOR[config.LAYER_OPTIONAL]),
            ("accepted", STATUS_COLOR["已采纳"]),
            ("rejected", STATUS_COLOR["已拒绝"]),
            (OTHER_LAYER_SLUG, OTHER_LAYER_COLOR),
        )
    )
    st.markdown(f"<style>\n{rules}\n</style>", unsafe_allow_html=True)


def inject_sidebar_brand() -> None:
    """在侧边栏顶部放一个品牌标识（色块logo+名称），替代之前主区那个和页面标题重复的
    巨型 st.title。顺带压掉侧边栏默认的顶部留白，让品牌贴着顶部。
    """
    st.markdown(
        "<style>section[data-testid='stSidebar'] div[data-testid='stSidebarHeader']"
        "{padding-bottom:0}</style>",
        unsafe_allow_html=True,
    )
    st.sidebar.markdown(
        "<div style='display:flex;align-items:center;gap:10px;padding:4px 0 12px'>"
        "<div style='width:34px;height:34px;border-radius:8px;background:#2C5578;"
        "color:#fff;display:flex;align-items:center;justify-content:center;"
        "font-weight:700;font-size:1.05rem'>校</div>"
        "<div style='font-size:1.15rem;font-weight:700;color:#1A1A1A'>校对助手</div>"
        "</div>",
        unsafe_allow_html=True,
    )
