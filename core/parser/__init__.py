"""文档解析模块。

统一解析三类输入：
  A类：有文字层的 PDF —— 见 native_pdf.py。
  B类：无文字层的扫描 PDF —— 见 ocr_pdf.py。
  C类：Word 文档 —— 见 docx_parser.py。

只做解析，不分块、不接校对LLM。详细设计背景（分栏检测判定、OCR相关细节、
编码踩坑记录）见本目录下 CLAUDE.md。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import fitz  # PyMuPDF

import config
from core.parser._types import (
    NoTextLayerError,
    ParsedBlock,
    ParsedDocument,
    UnsupportedFormatError,
)
from core.parser._common import _source_location
from core.parser.docx_parser import parse_docx as _parse_docx
from core.parser.native_pdf import _extract_native_page_raw, _finalize_native_page, _strip_headers_footers
from core.parser.ocr_pdf import _maybe_split_spread, _ocr_and_order, _render_page_image

__all__ = [
    "ParsedBlock",
    "ParsedDocument",
    "UnsupportedFormatError",
    "NoTextLayerError",
    "parse_document",
]


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def parse_document(
    file_path: str | Path,
    force_layout: str = "auto",   # 'auto'/'single'/'double'：手动强制指定分栏方式，跳过自动检测
    spread_order: str = "normal", # 跨页(两页拼一张图)拆分后的阅读顺序：'normal'先左后右 / 'cover_first'封面优先
    ocr: str = "auto",            # 'auto'按有无文字层自动判断 / 'force'强制全部OCR / 'off'禁用OCR（无文字层则报错）
) -> ParsedDocument:
    """对外唯一入口：按文件后缀分派到对应解析通道，返回统一的 ParsedDocument。"""
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _parse_docx(path)
    if suffix == ".pdf":
        return _parse_pdf(path, force_layout=force_layout, spread_order=spread_order, ocr=ocr)
    raise UnsupportedFormatError(f"不支持的文件格式：{suffix}")


# ---------------------------------------------------------------------------
# PDF 总入口：逐页判断文字层，分派到 A类/B类通道
# ---------------------------------------------------------------------------

def _parse_pdf(path: Path, force_layout: str, spread_order: str, ocr: str) -> ParsedDocument:
    """PDF 总入口：逐"物理页"判断有无文字层，分派到A类(原生提取)或B类(OCR)通道，
    最后把两类结果按逻辑页顺序合并成一份 ParsedDocument。

    这里区分"物理页"和"逻辑页"：一个物理页如果是跨页扫描（两页拼在一张图里），
    会在B类通道里被拆成两个逻辑页（见 _maybe_split_spread）；因此物理页数和
    最终 total_pages（逻辑页数）可能不相等。
    """
    doc = fitz.open(str(path))
    warnings: list[str] = []
    logical_pages: list[dict] = []  # 每个逻辑页一条记录：{"source","page_no","mode","blocks"}，按顺序追加
    # A类（原生）页面先收集原始块，不在循环里立即处理页眉页脚——因为页眉页脚要
    # 跨多页比较重复文本才能识别，必须等所有原生页都提取完再统一处理（见下方 _strip_headers_footers）
    native_pending: list[tuple[dict, list[dict], float]] = []  # (占位dict, 原始块列表, 页宽)

    logical_page_no = 0
    try:
        for phys_index in range(doc.page_count):
            page = doc[phys_index]
            # 用能提取到的字符数判断这一页有没有"文字层"：字符数太少（哪怕能提取出几个）
            # 通常是图片扫描件里嵌的水印/页码之类，不能算真正有文字层
            char_count = len(page.get_text().strip())
            page_is_native = char_count >= config.TEXT_LAYER_MIN_CHARS

            if ocr == "off" and not page_is_native:
                raise NoTextLayerError(f"第{phys_index + 1}页无文字层，且 ocr='off' 不允许OCR")

            # ocr 参数三种取值的优先级：force 强制OCR > off 强制禁用 > auto 按实际情况判断
            if ocr == "force":
                use_ocr = True
            elif ocr == "off":
                use_ocr = False
            else:
                use_ocr = not page_is_native

            if not use_ocr:
                # ---- A类通道：原生文字层，直接按坐标提取文本块 ----
                logical_page_no += 1
                # allow_ocr 只影响"被编造的字形要不要渲染送OCR读回真身"（见
                # _glyph_repair.py）：ocr='off' 的语义是这次调用不许碰OCR，字形还原也算在内。
                raw_blocks, width = _extract_native_page_raw(page, allow_ocr=(ocr != "off"))
                # 先放一个占位dict进最终结果列表里占好位置（保证页面顺序），
                # mode/blocks 留空，等所有原生页收集完、统一做完页眉页脚剔除后再回填
                placeholder: dict = {"source": "native", "page_no": logical_page_no, "mode": None, "blocks": None}
                logical_pages.append(placeholder)
                native_pending.append((placeholder, raw_blocks, width))
            else:
                # ---- B类通道：无文字层，走图片渲染 + OCR ----
                img = _render_page_image(page, config.OCR_RENDER_DPI)
                # 一个物理页可能其实是"两页拼一张"的跨页扫描，这里先检测并拆分成
                # 一到两张子图（正常情况下 sub_images 只有一张，即原图本身）
                sub_images = _maybe_split_spread(img, spread_order, warnings, phys_index + 1)
                for sub_img in sub_images:
                    logical_page_no += 1
                    # OCR识别 + 版面分析 + 阅读顺序还原（含分栏判断），一步做完
                    raw_blocks, page_conf, mode = _ocr_and_order(sub_img, force_layout)
                    if page_conf is not None and page_conf < config.OCR_PAGE_LOW_CONFIDENCE_THRESHOLD:
                        warnings.append(f"第{logical_page_no}页整体OCR识别置信度偏低({page_conf:.2f})")
                    # 统一转换成和A类相同的中间字典结构，方便后面合并处理
                    finalized = [
                        {
                            "text": b["text"],
                            "block_type": b["block_type"],
                            "source_location": _source_location(logical_page_no, mode, b.get("column")),
                            "confidence": b["confidence"],
                        }
                        for b in raw_blocks
                    ]
                    logical_pages.append({"source": "ocr", "page_no": logical_page_no, "mode": mode, "blocks": finalized})
    finally:
        doc.close()  # 无论中途是否抛异常，都要关闭PDF文件句柄

    # 所有物理页扫完之后，统一处理原生页的页眉页脚剔除+分栏+阅读顺序还原
    # （必须放在循环外，因为页眉页脚判定要看"多页重复出现的同位置文本"这个跨页信息）
    if native_pending:
        stripped_list = _strip_headers_footers([rb for _, rb, _ in native_pending])
        for (placeholder, _, width), stripped in zip(native_pending, stripped_list):
            finalized, mode = _finalize_native_page(stripped, width, placeholder["page_no"], force_layout)
            # 回填之前占位时留空的 mode 和 blocks
            placeholder["mode"] = mode
            placeholder["blocks"] = finalized

    # ---- 把所有逻辑页（不分A类/B类）按顺序展开成最终的 ParsedBlock 列表 ----
    all_blocks: list[ParsedBlock] = []
    idx = 0
    text_sources: set[str] = set()   # 收集出现过的来源（native/ocr），判断整篇文档是否混合
    layout_modes: list[str] = []     # 收集每页的版式，之后取"多数版式"作为整篇文档的 layout_mode
    for lp in logical_pages:
        text_sources.add(lp["source"])
        layout_modes.append(lp["mode"])
        for b in lp["blocks"]:
            all_blocks.append(
                ParsedBlock(
                    page=lp["page_no"],
                    block_index=idx,
                    text=b["text"],
                    block_type=b["block_type"],
                    source_location=b["source_location"],
                    ocr_confidence=b["confidence"],
                )
            )
            idx += 1

    # 如果全篇页面来源只有一种（要么全native要么全ocr），直接用那个值；否则标为 'mixed'
    text_source = text_sources.pop() if len(text_sources) == 1 else "mixed"
    layout_mode = _majority_layout_mode(layout_modes)

    return ParsedDocument(
        file_name=path.name,
        file_type="pdf",
        total_pages=logical_page_no,   # 用逻辑页数（含跨页拆分后的页数），不是物理页数
        blocks=all_blocks,
        layout_mode=layout_mode,
        text_source=text_source,
        warnings=warnings,
    )


def _majority_layout_mode(page_modes: list[str]) -> str:
    """按多数页面的分栏结果汇总文档级 layout_mode。

    单页分栏判断是启发式的，个别页（如整页大表格/图片、正文块很少的页）
    偶尔会判断有误；只要多数页一致就采用多数结果，避免一两页误判就把
    全文档标成 'mixed'。只有多数页本身就分歧较大时才真正视为 'mixed'。
    """
    if not page_modes:
        return "single"
    counts = Counter(page_modes)    # 统计每种分栏模式出现次数
    mode, count = counts.most_common(1)[0]  # 取出现次数最多的分栏模式
    return mode if count / len(page_modes) > 0.5 else "mixed"
