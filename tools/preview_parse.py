"""阶段2调试辅助脚本：预览 core.parser.parse_document 的解析结果。

用法：
    python tools/preview_parse.py <file_path> [--force-layout auto|single|double]
                                                [--spread-order normal|cover_first]
                                                [--ocr auto|force|off]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.parser import parse_document  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="预览文档解析结果")
    parser.add_argument("file_path")
    parser.add_argument("--force-layout", default="auto", choices=["auto", "single", "double"])
    parser.add_argument("--spread-order", default="normal", choices=["normal", "cover_first"])
    parser.add_argument("--ocr", default="auto", choices=["auto", "force", "off"])
    args = parser.parse_args()

    doc = parse_document(
        args.file_path,
        force_layout=args.force_layout,
        spread_order=args.spread_order,
        ocr=args.ocr,
    )

    for b in doc.blocks:
        conf = f"{b.ocr_confidence:.2f}" if b.ocr_confidence is not None else "-"
        preview = b.text[:50].replace("\n", " ")
        print(f"[第{b.page}页][{b.source_location}][{b.block_type}][{conf}] {preview}")

    print("---")
    print(f"file_name={doc.file_name} file_type={doc.file_type} total_pages={doc.total_pages}")
    print(f"layout_mode={doc.layout_mode} text_source={doc.text_source}")
    print(f"warnings={doc.warnings}")


if __name__ == "__main__":
    main()
