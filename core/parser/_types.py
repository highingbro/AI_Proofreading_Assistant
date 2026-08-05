"""解析模块的公共数据结构与异常。

独立成文件是为了让 native_pdf.py/ocr_pdf.py/docx_parser.py 都能导入
ParsedBlock/ParsedDocument 而不必反向依赖 __init__.py，避免循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class UnsupportedFormatError(Exception):
    """不支持的文件格式。"""


class NoTextLayerError(Exception):
    """文档无文字层，且调用方要求禁用OCR（ocr='off'）。"""


@dataclass
class ParsedBlock:
    """解析出的最小文本单元（一个段落/一行标题/一个表格块等）。

    不区分来源（PDF原生提取 / OCR识别 / Word段落），三条解析通道
    最终都统一转换成这个结构，下游（分块、校对、导出）只需认这一种格式。
    """
    page: int              # 逻辑页码，从1开始；Word文档没有页的概念，统一填0
    block_index: int       # 在整篇文档中的顺序号，已按“正确阅读顺序”排列（不是原始提取顺序）
    text: str              # 该块的文本内容
    block_type: str  # 'paragraph'/'heading'/'footnote'/'table'/'figure_caption'/'other'
    source_location: str   # 人类可读的位置描述，如"第3页左栏"/"第5段"，方便追问/导出时定位
    ocr_confidence: float | None = None  # OCR识别置信度(0~1)；非OCR来源（原生PDF/Word）恒为None
    doc_page: str | None = None  # 双栏页从页眉/页脚提取到的期刊自身页码（如"12"），未提取到为None


@dataclass
class ParsedDocument:
    """一份文档的完整解析结果。"""
    file_name: str
    file_type: str  # 'pdf'/'docx'
    total_pages: int       # 逻辑页数；Word文档没有页，填段落总数
    blocks: list[ParsedBlock] = field(default_factory=list)  # 按阅读顺序排好的所有文本块
    layout_mode: str = "single"  # 'single'/'double'/'mixed'：整篇文档的版式（单栏/双栏/混合）
    text_source: str = "native"  # 'native'/'ocr'/'mixed'：文字是直接提取的还是OCR识别出来的
    warnings: list[str] = field(default_factory=list)  # 解析过程中的异常/低置信度提示，不阻断流程
