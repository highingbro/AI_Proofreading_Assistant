"""基础归层规则（互斥，先命中先生效，_rule_default 保底必命中）。

对应设计文档的规则A(引文保护)/B(事实置信度)/E(风格)/F(默认兜底)。
"""

from __future__ import annotations

import config
from core.classifier.heuristics import _has_factual_feature, _has_quotation_feature
from core.proofreader import RawIssue


def _rule_quotation(raw: RawIssue):
    notes = []
    if raw.category == "quotation":
        notes.append("LLM自报quotation")
    if raw.issue_type == "引用与成语准确性":
        notes.append("issue_type=引用与成语准确性")
    if _has_quotation_feature(raw.original_text):
        notes.append("original_text命中引文文本特征(书名号/长引号/文言虚词)")
    if not notes:
        return None
    suggestion = f"原文照录，不建议改动。LLM提示的疑点供参考：{raw.reason}"
    return config.LAYER_QUOTATION, config.PRIORITY_LOW, suggestion, notes


def _rule_factual(raw: RawIssue):
    notes = []
    if raw.category == "factual":
        notes.append("LLM自报factual")
    if raw.issue_type == "常识与事实性错误":
        notes.append("issue_type=常识与事实性错误")
    if _has_factual_feature(raw):
        notes.append("original_text命中人名/职务/机构名/年份特征")
    if not notes:
        return None

    if raw.confidence == "high":
        layer = config.LAYER_CONFIRMED
        suggestion = raw.suggestion
        notes.append("事实类high置信,建议人工复核仍然适用")
    else:
        layer = config.LAYER_DOUBTFUL
        # LLM自己对medium/low置信度问题的建议措辞里，有时已经自带"存疑，建议人工核实"类表述，
        # 直接拼接会产生"存疑,建议人工核实：存疑，建议人工核实：..."的重复话术，先判断再拼接。
        if raw.suggestion.strip().startswith("存疑"):
            suggestion = raw.suggestion
        else:
            suggestion = f"存疑,建议人工核实：{raw.suggestion}"
        notes.append(f"事实类{raw.confidence}置信,降级为存疑待核实")
    return layer, config.PRIORITY_MEDIUM, suggestion, notes


def _rule_style(raw: RawIssue):
    combined = f"{raw.suggestion}{raw.reason}"
    hit = raw.category == "style" or any(k in combined for k in config.STYLE_KEYWORDS)
    if not hit:
        return None
    return config.LAYER_OPTIONAL, config.PRIORITY_OPTIONAL, raw.suggestion, ["风格可选"]


def _rule_default(raw: RawIssue):
    if raw.confidence == "high":
        return (
            config.LAYER_CONFIRMED,
            config.PRIORITY_MEDIUM,
            raw.suggestion,
            ["默认归层:high置信度→确定性错误"],
        )
    return (
        config.LAYER_DOUBTFUL,
        config.PRIORITY_MEDIUM,
        raw.suggestion,
        [f"默认归层:{raw.confidence}置信度→存疑待核实"],
    )


_BASE_RULES = (_rule_quotation, _rule_factual, _rule_style, _rule_default)
