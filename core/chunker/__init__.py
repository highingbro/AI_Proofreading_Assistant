"""长文档分块模块。

把 core/parser 产出的 ParsedDocument 切成一组 Chunk，供 core/proofreader/
逐次LLM校对调用的正文。只在 block 边界切分（超长 block 例外），块间保留前向
重叠上下文，且每块都能回溯到原始 block_index，供问题定位/导出使用。

详细设计背景（table区域跳过规则的真实诊断数据）见 core/chunker/CLAUDE.md。
"""

from __future__ import annotations

import config
from core.chunker._types import Chunk, ChunkedDocument
from core.chunker.fill_units import _build_fill_units
from core.chunker.greedy_fill import _fill_body
from core.chunker.locate import chunk_for_block, locate_block
from core.parser import ParsedDocument

__all__ = ["Chunk", "ChunkedDocument", "chunk_document", "locate_block", "chunk_for_block"]


def chunk_document(
    parsed: ParsedDocument,
    chunk_size_target: int | None = None,
    overlap_blocks: int | None = None,
) -> ChunkedDocument:
    """将解析后的文档按边界切块，块间保留上下文重叠。"""
    target = config.CHUNK_SIZE_TARGET if chunk_size_target is None else chunk_size_target
    overlap_n = config.OVERLAP_BLOCKS if overlap_blocks is None else overlap_blocks
    max_size = config.CHUNK_SIZE_MAX

    warnings: list[str] = []
    block_by_index = {b.block_index: b for b in parsed.blocks}
    fill_units = _build_fill_units(parsed.blocks, max_size, warnings)

    chunks: list[Chunk] = []
    prev_group = []
    i = 0
    chunk_index = 0
    n = len(fill_units)

    while i < n:
        # 重叠区文本取自"上一块正文实际用到的填充单元片段"，而不是重新按
        # block_index 去查原始block全文——超长block被切分为多个片段时，
        # 后者会把整个超长block的全文都塞进重叠区，是错误行为。
        if chunk_index == 0 or overlap_n <= 0 or not prev_group:
            # 首块、未启用重叠、或上一块为空时，本块没有重叠前缀
            overlap_ids: list[int] = []
            overlap_text = ""
        else:
            # dict.fromkeys 去重同时保序，得到上一块正文涉及的 block_index 顺序列表
            prev_block_indices = list(dict.fromkeys(u.block_index for u in prev_group))
            # 只取上一块末尾 overlap_n 个 block 作为本块的重叠上下文
            overlap_ids = prev_block_indices[-overlap_n:]
            overlap_id_set = set(overlap_ids)
            # 从 prev_group 的填充单元里取文本，而非重新查原始block全文
            overlap_text = "".join(u.text for u in prev_group if u.block_index in overlap_id_set)

        if overlap_ids:
            overlap_wrap = f"{config.CHUNK_OVERLAP_MARK}\n{overlap_text}\n{config.CHUNK_BODY_MARK}\n"
        else:
            overlap_wrap = ""

        # 重叠区包装文本也占字符预算，正文可用空间要相应扣除
        effective_target = target - len(overlap_wrap)
        group, i = _fill_body(fill_units, i, effective_target)

        block_indices = list(dict.fromkeys(u.block_index for u in group))
        # 不同 block 之间插入换行，避免LLM看到的正文在block接缝处完全无缝——
        # 各 ParsedBlock.text 在解析阶段已 strip 首尾空白（core/parser/），
        # 空字符串拼接会让相邻block的文字直接连在一起，导致LLM摘录的
        # original_text 有概率横跨两个block，而 core/proofreader/locator.py
        # 逐block定位时用的是未拼接的原始 block.text，找不到跨block的片段，
        # 产出"命中正文但未能归属到具体block"的告警。同一超长block切分出的
        # 多个片段共享同一 block_index，之间仍不加分隔符，保持能拼回原始
        # block.text 原样。
        body_parts: list[str] = []
        prev_block_index: int | None = None
        for u in group:
            if body_parts and u.block_index != prev_block_index:
                body_parts.append("\n")
            body_parts.append(u.text)
            prev_block_index = u.block_index
        body_text = "".join(body_parts)
        text = overlap_wrap + body_text

        # 页码范围覆盖重叠区和正文两部分，取两者最小页与最大页
        pages = [block_by_index[bi].page for bi in overlap_ids] + [u.page for u in group]
        char_count = len(text)
        if char_count > max_size:
            warnings.append(
                f"第{chunk_index}块字符数{char_count}超过硬上限{max_size}"
                "（重叠区与超长block切分片段叠加导致，已如实保留，未强行二次收缩）"
            )

        chunks.append(
            Chunk(
                chunk_index=chunk_index,
                text=text,
                block_indices=block_indices,
                overlap_prefix_blocks=overlap_ids,
                page_range=(min(pages), max(pages)),
                char_count=char_count,
            )
        )
        prev_group = group
        chunk_index += 1

    return ChunkedDocument(
        source=parsed,
        chunks=chunks,
        chunk_size_target=target,
        overlap_blocks=overlap_n,
        warnings=warnings,
    )
