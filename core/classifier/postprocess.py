"""跨块去重 / 排序 / 统计。"""

from __future__ import annotations

import logging
import re

import config
from core.classifier._types import ClassifiedIssue

logger = logging.getLogger(__name__)

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
    """block_index 本身已是阶段2按阅读顺序分配的全局序号，升序排列即满足
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
