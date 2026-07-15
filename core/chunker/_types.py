"""长文档分块模块的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.parser import ParsedDocument


@dataclass
class Chunk:
    chunk_index: int              # 分块序号，从0开始
    text: str                     # 送给LLM的正文（由若干block的text拼接，含重叠区包装）
    block_indices: list[int]      # 本块正文包含的 ParsedBlock.block_index 列表（不含重叠区）
    overlap_prefix_blocks: list[int]  # 前向重叠区的 block_index（仅作上下文，其上问题不在本块报告）
    page_range: tuple[int, int]   # 本块覆盖的逻辑页码范围（含重叠区）
    char_count: int               # text 字符数


@dataclass
class ChunkedDocument:
    source: ParsedDocument        # 引用原解析结果
    chunks: list[Chunk] = field(default_factory=list)
    chunk_size_target: int = 0    # 本次分块使用的目标块大小
    overlap_blocks: int = 0       # 本次使用的重叠块数
    warnings: list[str] = field(default_factory=list)
