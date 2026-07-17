"""原文定位回填：把LLM回填的 original_text 片段定位回具体 block。"""

from __future__ import annotations

import re

import config
from core.chunker import Chunk
from core.parser import ParsedDocument

def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _contains(haystack: str, needle: str) -> bool:
    """先精确子串匹配，失败则去空白后模糊匹配。"""
    if needle in haystack:
        return True
    return _normalize_ws(needle) in _normalize_ws(haystack)


def _split_body_overlap(chunk_text: str) -> tuple[str, str]:
    """把 chunk.text 拆成 (正文区文本, 重叠区文本)。无重叠区时overlap为空字符串。"""
    if chunk_text.startswith(config.CHUNK_OVERLAP_MARK):
        marker = config.CHUNK_BODY_MARK + "\n"
        overlap_part, sep, body_part = chunk_text.partition(marker)
        if sep:
            overlap_text = overlap_part[len(config.CHUNK_OVERLAP_MARK) + 1 :]
            return body_part, overlap_text
    return chunk_text, ""


def _locate_block_for_snippet(snippet: str, chunk: Chunk, parsed: ParsedDocument) -> int | None:
    """先按单block定位；单block找不到时，尝试拼接相邻若干个block兜底。

    起因：版面检测偶尔会把肉眼看是同一个自然段的内容拆成两个相邻block
    （真实诊断样本里全部是带项目符号""换行的段落，如"...考试不通\n"/
    "过可以重复参加考试。"被拆成block_index相邻的两个block）——LLM看到的
    chunk正文是连续的，摘出的original_text会横跨这两个block，逐block单独
    比对时两边都不完整包含它。窗口最多探到3个连续block（真实样本全部是
    2个block的拆分，留一档余量），命中时定位到窗口第一个block（原文的
    起始位置），保证 page_location/context_snippet 仍落在有意义的位置。
    """
    block_by_index = {b.block_index: b for b in parsed.blocks}
    indices = chunk.block_indices
    for window in (1, 2, 3):
        for start in range(len(indices) - window + 1):
            window_ids = indices[start : start + window]
            blocks = [block_by_index[bi] for bi in window_ids if bi in block_by_index]
            if len(blocks) != window:
                continue
            joined = "".join(b.text for b in blocks)
            if _contains(joined, snippet):
                return window_ids[0]
    return None
