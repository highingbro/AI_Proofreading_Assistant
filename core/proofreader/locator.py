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
    block_by_index = {b.block_index: b for b in parsed.blocks}
    for bi in chunk.block_indices:
        block = block_by_index.get(bi)
        if block is not None and _contains(block.text, snippet):
            return bi
    return None
