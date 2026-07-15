"""阶段3调试辅助脚本：预览 core.chunker.chunk_document 的分块结果。

用法：
    python tools/preview_chunks.py <file_path> [--chunk-size N] [--overlap-blocks N] [--dump N]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.chunker import chunk_document  # noqa: E402
from core.parser import parse_document  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="预览长文档分块结果")
    parser.add_argument("file_path")
    parser.add_argument("--chunk-size", type=int, default=None, help="目标块大小（字符数），默认取 config.CHUNK_SIZE_TARGET")
    parser.add_argument("--overlap-blocks", type=int, default=None, help="重叠block数，默认取 config.OVERLAP_BLOCKS")
    parser.add_argument("--dump", type=int, default=None, help="完整打印第N块（从0开始）的text")
    args = parser.parse_args()

    parsed = parse_document(args.file_path)
    chunked = chunk_document(parsed, chunk_size_target=args.chunk_size, overlap_blocks=args.overlap_blocks)

    for c in chunked.chunks:
        head = c.text[:20].replace("\n", " ")
        tail = c.text[-20:].replace("\n", " ")
        print(
            f"[块{c.chunk_index}] 页{c.page_range[0]}~{c.page_range[1]} | "
            f"正文blocks={len(c.block_indices)} | 重叠blocks={len(c.overlap_prefix_blocks)} | "
            f"字符数={c.char_count} | {head}…{tail}"
        )

    char_counts = [c.char_count for c in chunked.chunks]
    print("---")
    if char_counts:
        print(f"总块数={len(chunked.chunks)} 平均字符数={sum(char_counts) / len(char_counts):.0f} 最大字符数={max(char_counts)}")
    else:
        print("总块数=0")
    print(f"chunk_size_target={chunked.chunk_size_target} overlap_blocks={chunked.overlap_blocks}")
    print(f"warnings={chunked.warnings}")

    if args.dump is not None:
        target_chunk = chunked.chunks[args.dump]
        print("=" * 40)
        print(f"[块{args.dump}] 完整text:")
        print(target_chunk.text)


if __name__ == "__main__":
    main()
