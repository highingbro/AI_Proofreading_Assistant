"""基础归层规则（互斥，先命中先生效，_rule_default 保底必命中）。

对应设计文档的规则A(引文保护)/B(事实置信度)/E(风格)/F(默认兜底)，以及两条精简模式规则
_rule_simplified_grammar_as_style/_rule_simplified_typo_as_punctuation（详见
core/classifier/CLAUDE.md）。
"""

from __future__ import annotations

import config
from core.classifier.heuristics import _has_factual_feature, _has_quotation_feature
from core.proofreader import RawIssue

# LLM自报的confidence原始取值是英文字面量("high"/"medium"/"low")，layer_notes面向
# 人工阅读，统一转成中文，不要在中文说明里混入英文单词。
_CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}


def _confidence_label(confidence: str) -> str:
    return _CONFIDENCE_LABELS.get(confidence, confidence)


def _rule_quotation(raw: RawIssue, mode: str):
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


def _rule_factual(raw: RawIssue, mode: str):
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
        notes.append("事实类高置信,建议人工复核仍然适用")
    else:
        layer = config.LAYER_DOUBTFUL
        # LLM自己对medium/low置信度问题的建议措辞里，有时已经自带"存疑，建议人工核实"类表述，
        # 直接拼接会产生"存疑,建议人工核实：存疑，建议人工核实：..."的重复话术，先判断再拼接。
        if raw.suggestion.strip().startswith("存疑"):
            suggestion = raw.suggestion
        else:
            suggestion = f"存疑,建议人工核实：{raw.suggestion}"
        notes.append(f"事实类{_confidence_label(raw.confidence)}置信,降级为存疑待核实")
    return layer, config.PRIORITY_MEDIUM, suggestion, notes


def _rule_simplified_grammar_as_style(raw: RawIssue, mode: str):
    """精简模式：语法结构问题(规则2)统一按风格可选处理，不再判定确定性错误/存疑待核实。

    精简模式服务的是容忍度较高的普通/技术文档，"语法结构问题"命中率不低，
    但常常是"怎么写都通"的润色（典型信号：LLM建议里出现"改为A或B"这种平级备选写法，
    说明没有唯一正确写法），走 _rule_default 兜底判成确定性错误/存疑待核实过于严格。
    深度模式不受影响；引文/事实保护（_rule_quotation/_rule_factual）排在此规则之前，
    命中时仍优先生效，不会被这条规则弱化。
    """
    if mode != config.PROOFREAD_MODE_SIMPLIFIED:
        return None
    if raw.issue_type != "语法结构问题":
        return None
    return (
        config.LAYER_OPTIONAL,
        config.PRIORITY_OPTIONAL,
        raw.suggestion,
        ["精简模式下语法结构问题统一按风格可选处理"],
    )


def _rule_simplified_typo_as_punctuation(raw: RawIssue, mode: str):
    """精简模式：规则1(错别字与拼写)里"汉字冒充标点符号"这类零歧义问题按风格可选处理。

    起因：如数词"一"被当成破折号/连接号使用，字形近似但不是标点混用，读者理解完全不受影响，
    是纯排版惯例问题，和"的/地/得"这类可能真正改变语义/引起误解的错别字不是一回事，精简模式
    下不该同等严格对待。只用建议措辞是否命中连接类标点关键词识别（config.SIMPLIFIED_TYPO_
    PUNCTUATION_KEYWORDS），命中才降级；宁可漏判也不误伤真正的错别字。深度模式不受影响。
    """
    if mode != config.PROOFREAD_MODE_SIMPLIFIED:
        return None
    if raw.issue_type != "错别字与拼写":
        return None
    combined = f"{raw.suggestion}{raw.reason}"
    if not any(k in combined for k in config.SIMPLIFIED_TYPO_PUNCTUATION_KEYWORDS):
        return None
    return (
        config.LAYER_OPTIONAL,
        config.PRIORITY_OPTIONAL,
        raw.suggestion,
        ["精简模式下'汉字冒充标点符号'类错别字问题按风格可选处理"],
    )


def _rule_style(raw: RawIssue, mode: str):
    combined = f"{raw.suggestion}{raw.reason}"
    hit = raw.category == "style" or any(k in combined for k in config.STYLE_KEYWORDS)
    if not hit:
        return None
    return config.LAYER_OPTIONAL, config.PRIORITY_OPTIONAL, raw.suggestion, ["风格可选"]


def _rule_default(raw: RawIssue, mode: str):
    if raw.confidence == "high":
        return (
            config.LAYER_CONFIRMED,
            config.PRIORITY_MEDIUM,
            raw.suggestion,
            ["默认归层:高置信度→确定性错误"],
        )
    return (
        config.LAYER_DOUBTFUL,
        config.PRIORITY_MEDIUM,
        raw.suggestion,
        [f"默认归层:{_confidence_label(raw.confidence)}置信度→存疑待核实"],
    )


_BASE_RULES = (
    _rule_quotation,
    _rule_factual,
    _rule_simplified_grammar_as_style,
    _rule_simplified_typo_as_punctuation,
    _rule_style,
    _rule_default,
)
