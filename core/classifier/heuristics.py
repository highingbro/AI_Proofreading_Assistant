"""引文/事实文本特征启发式：弥补LLM未自报 category 时的漏报场景。"""

from __future__ import annotations

import re

import config
from core.proofreader import RawIssue

_BOOK_TITLE_RE = re.compile(r"《[^《》]+》")
_QUOTE_PAIRS = (
    re.compile(r"“([^“”]+)”"),
    re.compile(r"‘([^‘’]+)’"),
    re.compile(r'"([^"]+)"'),
    re.compile(r"'([^']+)'"),
)
_YEAR_RE = re.compile(r"(?:19|20)\d{2}年?")


def _has_classical_feature(text: str) -> bool:
    """文言虚词密度+计数双条件达标才判定，避免"总之""也是"等现代汉语孤例误判。"""
    if not text:
        return False
    count = sum(text.count(ch) for ch in config.QUOTATION_CLASSICAL_PARTICLES)
    if count < config.QUOTATION_CLASSICAL_MIN_COUNT:
        return False
    return count / len(text) >= config.QUOTATION_CLASSICAL_DENSITY_THRESHOLD


def _has_quotation_feature(text: str) -> bool:
    if _BOOK_TITLE_RE.search(text):
        return True
    for pattern in _QUOTE_PAIRS:
        for match in pattern.finditer(text):
            if len(match.group(1)) >= config.QUOTATION_QUOTE_MIN_CHARS:
                return True
    return _has_classical_feature(text)


def _has_factual_feature(raw: RawIssue) -> bool:
    """人名/职务/机构名/年份 + 改写型建议，兜底LLM未自报factual的漏报场景。"""
    if raw.suggestion.strip() == raw.original_text.strip():
        return False
    text = raw.original_text
    if _YEAR_RE.search(text):
        return True
    if any(k in text for k in config.FACTUAL_TITLE_KEYWORDS):
        return True
    if any(k in text for k in config.FACTUAL_ORG_SUFFIXES):
        return True
    return False
