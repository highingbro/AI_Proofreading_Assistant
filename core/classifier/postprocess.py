"""跨块去重 / 排序 / 统计 / 视觉无实质改动的建议过滤。"""

from __future__ import annotations

import logging
import re
import unicodedata

import config
from core.classifier._types import ClassifiedIssue
from core.proofreader import RawIssue

logger = logging.getLogger(__name__)

# 只锚定"应改为/改为『X』"这个确定性改写措辞去找引号内文字，不是任意引号——"存疑，
# 建议人工核实：..."这类suggestion按提示词补充规则二的固定格式，惯例上会在句子里
# 重新引用原文做说明（如"...称『年节约成本超过50万元』..."），套用宽泛的"找引号"
# 规则会把这类正常的存疑issue误伤成"零改动"，见 core/classifier/CLAUDE.md。
_REPLACEMENT_SUGGESTION_RE = re.compile(r'(?:应改为|改为)[“"\']([^”"\']+)[”"\']')

# "『旧片段』应改为『新片段』"这种只描述original_text里某个具体片段替换的措辞（常见于
# suggestion只想指出一个字/词有问题，而不是整个original_text都要换掉）——
# _REPLACEMENT_SUGGESTION_RE 那种"整段替换"比较方式在这种措辞下会因为新旧片段长度和
# original_text整体长度不一致而误判成"不是零改动"，需要单独识别这一对片段直接比较，
# 见 core/classifier/CLAUDE.md。
_FRAGMENT_REPLACEMENT_RE = re.compile(r'[“"\']([^”"\']+)[”"\'](?:应改为|改为)[“"\']([^”"\']+)[”"\']')

# PDF/OCR字体替代产生的部首/异体字形混入正文的真实案例（完整出处清单见
# core/classifier/CLAUDE.md）：CJK Radicals Supplement（U+2E80~2EFF）区块的字符
# 多数没有Unicode兼容分解，NFKC规范化处理不了；Kangxi Radicals（U+2F00~2FDF）区块
# 里的"⼾"虽然有兼容分解，但指向繁体"戶"，与简体正文实际需要的"户"不符。这里只收录
# 亲自核实过对应关系的字符，不做未验证的猜测性扩充——错误的映射表会静默篡改比对
# 结果，比漏判更危险。
_RADICAL_LOOKALIKE_OVERRIDES = {
    "⻔": "门", "⻩": "黄", "⻘": "青", "⻓": "长",
    "⻆": "角", "⻉": "贝", "⺟": "母", "⺠": "民",
    "⻋": "车", "⻨": "麦", "⻛": "风", "⻢": "马",
    "⼾": "户",
}

_CONSERVATISM_RANK = {
    config.LAYER_QUOTATION: 3,
    config.LAYER_DOUBTFUL: 2,
    config.LAYER_OPTIONAL: 1,
    config.LAYER_CONFIRMED: 0,
}

_STATS_LAYER_FIELD = {
    config.LAYER_CONFIRMED: "count_confirmed",
    config.LAYER_DOUBTFUL: "count_doubtful",
    config.LAYER_QUOTATION: "count_quotation",
    config.LAYER_OPTIONAL: "count_optional",
}


def _normalize_for_dedup(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _strip_whitespace(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _normalize_lookalike(text: str) -> str:
    """先替换已知的部首/异体字形替代字符（_RADICAL_LOOKALIKE_OVERRIDES），再做NFKC
    兼容规范化——两步合起来才能覆盖"有NFKC分解的部首"和"NFKC处理不了/分解到繁体
    的部首"两类真实出现过的字体替代场景。"""
    substituted = "".join(_RADICAL_LOOKALIKE_OVERRIDES.get(ch, ch) for ch in text)
    return unicodedata.normalize("NFKC", substituted)


def _extract_replacement_pair(raw: RawIssue) -> tuple[str, str] | None:
    """从suggestion里抽取"旧文本, 新文本"这一对，供零改动判定/空格差异判定共用。

    优先尝试"『旧』应改为『新』"片段式措辞（更具体，能处理original_text引用了更大
    上下文、真正的改动只是其中一个字/词的情况——比如original_text是"归⺟扣⾮净利润"
    整句，suggestion只说"『⺟』应改为『母』"）；抽取到的『旧』还要求在original_text
    里确实出现过，避免措辞不规范时抽到不相关的引用文本。抽不到片段式的再退回
    "应改为『新』"整段替换式措辞，与完整original_text比较。两种都抽不到返回None。
    """
    suggestion = raw.suggestion or ""
    original = (raw.original_text or "").strip()
    if not original:
        return None

    fragment_match = _FRAGMENT_REPLACEMENT_RE.search(suggestion)
    if fragment_match:
        old, new = fragment_match.group(1).strip(), fragment_match.group(2).strip()
        if old and new and old in original:
            return old, new

    whole_match = _REPLACEMENT_SUGGESTION_RE.search(suggestion)
    if whole_match:
        proposed = whole_match.group(1).strip()
        if proposed:
            return original, proposed

    return None


def _dedup(issues: list[ClassifiedIssue]) -> tuple[list[ClassifiedIssue], int]:
    """按 (block_index, 归一化original_text) 去重，保留更保守的一条。

    located=False 的条目没有可靠的 block_index，不参与去重，原样全部保留。
    """
    kept: dict[tuple[int, str], ClassifiedIssue] = {}
    order: list[tuple[int, str]] = []
    unlocated: list[ClassifiedIssue] = []
    dropped = 0

    for issue in issues:
        if issue.block_index is None:
            unlocated.append(issue)
            continue
        key = (issue.block_index, _normalize_for_dedup(issue.original_text))
        if key not in kept:
            kept[key] = issue
            order.append(key)
            continue
        existing = kept[key]
        dropped += 1
        if _CONSERVATISM_RANK[issue.layer] > _CONSERVATISM_RANK[existing.layer]:
            logger.info("去重: 用更保守的条目替换 block_index=%d 的重复问题: %s", issue.block_index, issue.original_text)
            kept[key] = issue
        else:
            logger.info("去重: 丢弃 block_index=%d 的重复问题: %s", issue.block_index, issue.original_text)

    result = [kept[k] for k in order] + unlocated
    return result, dropped


def _sort(issues: list[ClassifiedIssue]) -> list[ClassifiedIssue]:
    """block_index 本身已是 core/parser/ 按阅读顺序分配的全局序号，升序排列即满足
    "页码升序,同页按block_index"；未定位(None)的排最后。"""
    return sorted(issues, key=lambda i: (i.block_index is None, i.block_index if i.block_index is not None else 0))


def _compute_stats(issues: list[ClassifiedIssue]) -> dict:
    """字段名与 db/database.py 里 records 表列名对齐。"""
    stats = {
        "total_issues": len(issues),
        "count_confirmed": 0,
        "count_doubtful": 0,
        "count_quotation": 0,
        "count_optional": 0,
        "high_priority_count": 0,
    }
    for issue in issues:
        stats[_STATS_LAYER_FIELD[issue.layer]] += 1
        if issue.priority == config.PRIORITY_HIGH:
            stats["high_priority_count"] += 1
    return stats


def _is_visually_no_op_suggestion(raw: RawIssue) -> bool:
    """抽取出的"旧→新"文本（见 _extract_replacement_pair）经部首/异体字形替代修正+NFKC
    规范化后完全相同——说明这条建议实际上零改动：要么字面完全一样（LLM把"存疑"错报
    成了确定性建议却没给出真实改动），要么视觉相同但用了不同的Unicode码点/部首替代
    字形（如PDF解析产生的康熙部首"⽉"代替标准汉字"月"，或CJK部首补充区的"⻔"代替
    标准汉字"门"，肉眼看不出区别）。真实案例清单见 core/classifier/CLAUDE.md。
    """
    pair = _extract_replacement_pair(raw)
    if pair is None:
        return False
    old, new = pair
    if not old or not new:
        return False
    return _normalize_lookalike(old) == _normalize_lookalike(new)


def _has_stray_control_chars(text: str) -> bool:
    """original_text里出现\\t\\n\\r之外的控制字符（Unicode Cc类），只可能是PDF字体/
    字形解析错位产生的乱码（如私有区符号被误解析成SOH等控制码）——真实文档正文不会
    包含这类字符，不是"建议本身错了"，是这条issue引用的原文字段本身就是解析垃圾，
    没有可核实的价值，直接丢弃，不必进入归层/降级流程。
    """
    return any(unicodedata.category(ch) == "Cc" and ch not in "\t\n\r" for ch in text)


def _filter_visually_no_op(raw_issues: list[RawIssue]) -> tuple[list[RawIssue], int, int]:
    """丢弃两类没有核实价值的假问题，在 classify_issue 之前对 RawIssue 直接过滤、整条
    丢弃，不进入最终结果——与其余规则C/D/G/H/J"降级为存疑待核实、保留可审计性"的一般
    惯例不同：这两类已经确认没有人工核实的价值，用户明确要求直接从结果里丢弃，不走
    归层。

    1. 视觉/语义零改动（_is_visually_no_op_suggestion）。
    2. original_text本身含解析产生的控制字符乱码（_has_stray_control_chars）。

    返回 (保留的issues, 零改动丢弃数, 控制字符乱码丢弃数)。
    """
    kept = []
    no_op_dropped = 0
    control_char_dropped = 0
    for raw in raw_issues:
        if _has_stray_control_chars(raw.original_text or ""):
            control_char_dropped += 1
            logger.info("丢弃含解析乱码控制字符的问题: 原文=%r", raw.original_text)
            continue
        if _is_visually_no_op_suggestion(raw):
            no_op_dropped += 1
            logger.info("丢弃视觉无实质改动的建议: 原文=%s 建议=%s", raw.original_text, raw.suggestion)
            continue
        kept.append(raw)
    return kept, no_op_dropped, control_char_dropped
