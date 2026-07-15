"""C类：Word 文档解析（从 core/parser.py 拆分而来，逻辑未改动）。"""

from __future__ import annotations

from pathlib import Path

import docx

from core.parser._types import ParsedBlock, ParsedDocument


def parse_docx(path: Path) -> ParsedDocument:
    """Word 解析：直接按段落顺序读取，不涉及分栏/OCR，是三条通道里最简单的一条。"""
    document = docx.Document(str(path))
    blocks: list[ParsedBlock] = []
    idx = 0  # 剔除空段落后的顺序号，与下面循环里的 i（原始段落序号）不是同一个数
    for i, para in enumerate(document.paragraphs, start=1):
        text = para.text.strip()
        if not text:
            continue  # 空段落（纯换行/纯空格）直接跳过，不产出 ParsedBlock
        # para.style 是 Word 里作者手动设置的段落样式（点击"标题1"/"正文"等按钮时写入的元数据），
        # 不是内容分析结果。这里只做字符串匹配：样式名以"Heading"开头或等于"Title"就算标题，
        # 否则一律归为正文——如果作者没用 Word 内置标题样式，这里识别不出来。
        style_name = para.style.name if para.style is not None else ""
        block_type = "heading" if (style_name.startswith("Heading") or style_name == "Title") else "paragraph"
        blocks.append(
            ParsedBlock(
                page=0,               # Word 无页码概念，统一填0
                block_index=idx,
                text=text,
                block_type=block_type,
                source_location=f"第{i}段",  # 用原始段落号定位，方便用户在Word里核对
                ocr_confidence=None,   # Word 文本非OCR来源
            )
        )
        idx += 1
    return ParsedDocument(
        file_name=path.name,
        file_type="docx",
        total_pages=len(document.paragraphs),  # 用段落总数代替页数
        blocks=blocks,
        layout_mode="single",   # Word 文档不做分栏检测，固定单栏
        text_source="native",   # 固定为原生文本（非OCR）
        warnings=[],
    )
