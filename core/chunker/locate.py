"""定位辅助函数（供 core/classifier/、core/exporter.py 做问题定位回溯）。"""

from __future__ import annotations

from core.chunker._types import ChunkedDocument
from core.parser import ParsedDocument


def locate_block(parsed: ParsedDocument, block_index: int) -> str:
    """返回人类可读定位，如 '第3页左栏'（直接取 ParsedBlock.source_location）"""
    for b in parsed.blocks:
        if b.block_index == block_index:
            return b.source_location
    raise ValueError(f"未找到 block_index={block_index}")


def locate_doc_page(parsed: ParsedDocument, block_index: int) -> str | None:
    """返回该block所在页提取到的期刊页码（ParsedBlock.doc_page），未提取到为None。"""
    for b in parsed.blocks:
        if b.block_index == block_index:
            return b.doc_page
    raise ValueError(f"未找到 block_index={block_index}")


def chunk_for_block(chunked: ChunkedDocument, block_index: int) -> int:
    """返回某 block 所属（正文归属，非重叠区）的 chunk_index"""
    for chunk in chunked.chunks:
        if block_index in chunk.block_indices:
            return chunk.chunk_index
    raise ValueError(f"未找到 block_index={block_index} 所属的chunk")
