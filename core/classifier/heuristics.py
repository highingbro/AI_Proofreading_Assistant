"""引文/事实文本特征启发式：弥补LLM未自报 category 时的漏报场景。"""

from __future__ import annotations

import re

import config
from core.proofreader import RawIssue

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
    """不认书名号《》：真实数据（data/app.db 全部引文类issue离线回放）里 305 条引文类
    有 255 条只靠书名号命中，其中 239 条 LLM 想改的位置压根不在《》里面——序号与书名号
    之间的多余点、零宽字符、公示文件年份写错，全被"原文照录，不建议改动"压掉了。剩下
    16 条落在《》内部的也全是部首编码伪影（⾯向→面向），真引文一条没保住。根因是书名号
    标的是"作品名"而非"引文"，而正文里最常见的书名号用法是列举自家课程/文件标题，那是
    本方自己的原创内容，里面的错就是真错。
    """
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
