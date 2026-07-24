"""结果分层模块。

LLM 自报的 category/confidence 不可直接信任：本模块在系统层对 core/proofreader/
产出的 RawIssue 做强制归层校验（引文保护、事实置信度降级、OCR低置信度降级、未定位
降级、风格归层），即使 LLM 输出完全不守规则，最终结果也必须符合四层规范
（config.LAYER_* 四层）。纯规则逻辑，不调用LLM。

归层结构是"基础规则 + 修饰叠加"两层，详细设计背景（各条规则的判定原因、
去重/排序细节）见 core/classifier/CLAUDE.md。
"""

from __future__ import annotations

import config
from core.chunker import ChunkedDocument
from core.classifier._types import ClassifiedIssue, ClassifiedResult, _ClassificationState
from core.classifier.base_rules import _BASE_RULES
from core.classifier.modifier_rules import _MODIFIER_RULES, _compute_chunk_tail_blocks
from core.classifier.postprocess import _compute_stats, _dedup, _filter_visually_no_op, _sort
from core.parser import ParsedBlock, ParsedDocument
from core.proofreader import ProofreadResult, RawIssue

__all__ = ["ClassifiedIssue", "ClassifiedResult", "classify_issue", "classify_issues"]


# ---------------------------------------------------------------------------
# 单条分类
# ---------------------------------------------------------------------------

def classify_issue(
    raw: RawIssue,
    block_by_index: dict[int, ParsedBlock],
    tail_blocks: set[int] = frozenset(),
    mode: str = config.PROOFREAD_MODE_DEEP,
) -> ClassifiedIssue:
    """对单条 RawIssue 做归层校验，返回带分层标注的 ClassifiedIssue。

    mode 控制精简模式下语法结构问题(规则2)统一按风格可选处理这一条基础规则是否生效，
    不传时按深度模式（现状）处理，见 base_rules.py::_rule_simplified_grammar_as_style。
    """
    layer = priority = suggestion = None
    notes: list[str] = []
    for rule in _BASE_RULES:
        result = rule(raw, mode)
        if result is not None:
            layer, priority, suggestion, notes = result
            break

    block = block_by_index.get(raw.block_index) if raw.block_index is not None else None
    state = _ClassificationState(layer=layer, priority=priority, suggestion=suggestion, notes=notes)
    for _name, rule in _MODIFIER_RULES:
        state = rule(raw, block, tail_blocks, state)

    return ClassifiedIssue(
        original_text=raw.original_text,
        issue_type=raw.issue_type,
        suggestion=state.suggestion,
        reason=raw.reason,
        block_index=raw.block_index,
        page_location=raw.page_location,
        chunk_index=raw.chunk_index,
        located=raw.located,
        layer=state.layer,
        priority=state.priority,
        layer_notes=state.notes,
        llm_category=raw.category,
        llm_confidence=raw.confidence,
        original_suggestion=raw.suggestion,
    )


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def classify_issues(
    result: ProofreadResult,
    parsed: ParsedDocument,
    chunked: ChunkedDocument | None = None,
    mode: str = config.PROOFREAD_MODE_DEEP,
) -> ClassifiedResult:
    """对整份 ProofreadResult 做归层校验、跨块去重、排序并汇总统计。

    chunked 非空时启用规则H（分块边界截断误判豁免）；不传时（如离线用RawIssue JSON
    调规则的调试场景）该规则不生效，其余规则不受影响。mode 透传给每条 classify_issue，
    不传时按深度模式（现状）处理。

    _filter_visually_no_op 在归层之前先丢弃两类没有核实价值的假问题：建议改写内容
    和原文做部首/异体字形替代修正+Unicode规范化后完全相同的issue（零改动的假问题，
    如PDF解析产生的康熙部首代替标准汉字，肉眼看不出区别却被判成"错别字"），以及
    original_text本身含解析产生的控制字符乱码的issue，详见 core/classifier/CLAUDE.md。
    """
    block_by_index = {b.block_index: b for b in parsed.blocks}
    tail_blocks = _compute_chunk_tail_blocks(chunked)

    raw_issues, no_op_dropped, control_char_dropped = _filter_visually_no_op(result.issues)
    classified = [classify_issue(raw, block_by_index, tail_blocks, mode) for raw in raw_issues]
    deduped, dropped = _dedup(classified)
    ordered = _sort(deduped)
    stats = _compute_stats(ordered)

    warnings = list(result.chunk_warnings)
    if no_op_dropped:
        warnings.append(f"丢弃{no_op_dropped}条视觉无实质改动的建议")
    if control_char_dropped:
        warnings.append(f"丢弃{control_char_dropped}条解析产生乱码字符的问题")
    if dropped:
        warnings.append(f"跨块去重丢弃{dropped}条重复问题")

    return ClassifiedResult(issues=ordered, stats=stats, warnings=warnings)
