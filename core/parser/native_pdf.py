"""A类：有文字层 PDF 解析（从 core/parser.py 拆分而来，逻辑未改动）。

用 PyMuPDF 按坐标提取文本块，手写页眉页脚剔除 + 分栏检测 + 阅读顺序还原。
"""

from __future__ import annotations

import re
import statistics
from collections import Counter

import config
from core.parser._common import _detect_column_split, _source_location

_NUMERIC_ZONE_RE = re.compile(r"^[\dIVXLCDMivxlcdm\-\.\s]{1,10}$")      # 匹配纯页码/罗马数字页码/带破折号的页码范围/带逗号，长度不超过10字符


def _zone_of(bbox: tuple[float, float, float, float], height: float) -> str:
    """判断一个文本块的坐标框(bbox)落在页面的顶部/底部/正文区。"""
    y0, y1 = bbox[1], bbox[3]  # bbox = (x0, y0, x1, y1)，y0是上边界，y1是下边界
    if y1 <= height * config.HEADER_FOOTER_ZONE_RATIO:
        return "top"     # 整个块都在页面顶部8%范围内 → 可能是页眉
    if y0 >= height * (1 - config.HEADER_FOOTER_ZONE_RATIO):
        return "bottom"  # 整个块都在页面底部8%范围内 → 可能是页脚
    return "body"         # 其余都算正文区域


def _extract_native_page_raw(page: "fitz.Page") -> tuple[list[dict], float]:
    """从有文字层的PDF页面里，按PyMuPDF给出的坐标提取所有文本块的原始信息。

    这里只是"提取"，不做分栏、不做阅读顺序还原、不剔除页眉页脚——
    那些都是后续步骤（_strip_headers_footers / _finalize_native_page）的事。
    """
    data = page.get_text("dict")  # PyMuPDF 提取结果，是一棵 blocks→lines→spans 的嵌套结构
    width, height = data["width"], data["height"]
    out: list[dict] = []
    for b in data["blocks"]:
        if b.get("type") != 0:
            continue  # type!=0 是图片块，跳过（只保留文字块）
        lines_text = []
        sizes = []
        for line in b["lines"]:
            # 一个block可能包含多行(line)，每行又由多个span组成（比如中途换了字体/字号）
            t = "".join(s["text"] for s in line["spans"])
            if t.strip():
                lines_text.append(t.strip())
            sizes.extend(s["size"] for s in line["spans"])  # 记录这一行里每个span的字号
        # 用换行符而非空格拼接：block内每一行在原文里就是独立的一行（可能是标题+正文、
        # 列表项、版式换行等不同语义单元），拼成空格会让LLM把它们读成一句连续的话，
        # 导致把"排版换行"误判成"语法/标点问题"（换行标记本身不新增block，不改变
        # block_index颗粒度，下游分块/去重/追问上下文窗口都不受影响）。
        text = "\n".join(lines_text)
        if not text:
            continue
        avg_size = sum(sizes) / len(sizes) if sizes else 0.0  # 这个block的平均字号，供后续判断是否为标题用
        bbox = tuple(b["bbox"])
        out.append({"text": text, "bbox": bbox, "avg_size": avg_size, "zone": _zone_of(bbox, height)})
    return out, width


# pages_raw: list[list[dict]]
#   外层list：多个页面，每个元素是"一页"
#   内层list：这一页里的所有文本块
#   dict：每个文本块本身，即 {"text":, "bbox":, "avg_size":, "zone":}
def _strip_headers_footers(pages_raw: list[list[dict]]) -> list[list[dict]]:
    """跨页剔除重复出现的页眉/页脚文本，以及页码类文本。

    传入的是多页的原始块列表（每页一个list），因为判断"是否是页眉页脚"
    必须对比多页——单看一页无法区分"页眉"和"恰好在页面顶部的正文标题"。
    """
    # 第一遍：统计每一段"位于顶部/底部区域"的文本，在多少个不同页面里出现过
    zone_text_counter: Counter[str] = Counter()
    for page_raw in pages_raw:
        seen_this_page = set()  # 同一页内即使同一段文字出现两次，也只算一次（避免重复计数）
        for blk in page_raw:
            if blk["zone"] in ("top", "bottom") and blk["text"] not in seen_this_page:
                zone_text_counter[blk["text"]] += 1
                seen_this_page.add(blk["text"])
    # 在2页或以上的顶/底部区域重复出现的文本，判定为页眉页脚
    repeated = {t for t, c in zone_text_counter.items() if c >= 2}

    # 第二遍：逐页过滤，剔除"重复文本"和"纯页码/罗马数字页码"（_NUMERIC_ZONE_RE）
    result = []
    for page_raw in pages_raw:
        kept = [
            blk
            for blk in page_raw
            if not (blk["zone"] in ("top", "bottom") and (blk["text"] in repeated or _NUMERIC_ZONE_RE.match(blk["text"])))
        ]
        result.append(kept)
    return result


def _classify_native_block_type(avg_size: float, body_size: float, zone: str) -> str:
    """按字号跟正文字号的比例，猜这个块是标题/脚注/正文（原生PDF没有Word那种样式元数据，
    只能靠视觉特征——字号大小——来判断，跟 docx_parser.parse_docx 里读 style_name 的思路完全不同）。
    """
    if body_size and avg_size > body_size * 1.15:
        return "heading"   # 字号比正文大15%以上 → 判定为标题
    if zone == "bottom" and body_size and avg_size < body_size * 0.9:
        return "footnote"  # 位于页面底部区域 且 字号比正文小10%以上 → 判定为脚注（脚注通常字小、位置靠下）
    return "paragraph"     # 其余都算正文


def _order_native_page(raw_blocks: list[dict], width: float, force_layout: str) -> tuple[list[dict], str]:
    """把一页里提取出来的文本块，按"正确阅读顺序"重新排列，并判断这页是单栏还是双栏。

    返回 (排好序的块列表, 'single'或'double')。
    """
    # 先决定这页是单栏还是双栏：force_layout 手动指定的话直接用；否则用自动检测
    if force_layout == "single":
        mode = "single"
    elif force_layout == "double":
        mode = "double"
    else:
        split_x = _detect_column_split(raw_blocks, width)
        mode = "double" if split_x is not None else "single"

    if mode == "single":
        # 单栏很简单：直接按y坐标（bbox[1]，即上边界）从上到下排序即可
        ordered = sorted(raw_blocks, key=lambda b: b["bbox"][1])
        for b in ordered:
            b["column"] = None  # 单栏没有栏位概念，统一置空，跟双栏分支保持字段结构一致
        return ordered, mode

    # ---- 双栏排序逻辑 ----
    # 上面为了决定mode已经调用过一次_detect_column_split，这里再调一次是为了拿到具体的
    # 分栏线x坐标split_x（上面那次调用只用了它的返回值是否为None，没保留具体数值）
    split_x = _detect_column_split(raw_blocks, width)
    if split_x is None:
        split_x = width / 2  # 双栏但没找到精确分栏线（如force_layout强制指定双栏）时，退化为对半分

    col_width_est = width / 2  # 估算单栏应有的宽度，用于判断块是否"跨栏"
    spanning, left, right = [], [], []  # 三类块：跨两栏的、左栏的、右栏的
    for b in raw_blocks:
        x0, _, x1, _ = b["bbox"]
        if (x1 - x0) > col_width_est * 1.4:
            # 块宽度明显超过单栏宽度（超过1.4倍），说明它横跨了左右两栏（如通栏标题）
            spanning.append(b)
        else:
            center = (x0 + x1) / 2
            if center < split_x:
                b["column"] = "left"
                left.append(b)
            else:
                b["column"] = "right"
                right.append(b)

    # 左栏、右栏、跨栏块各自按y坐标（从上到下）排序
    left.sort(key=lambda b: b["bbox"][1])
    right.sort(key=lambda b: b["bbox"][1])
    spanning.sort(key=lambda b: b["bbox"][1])
    for b in spanning:
        b["column"] = "span"

    if not spanning:
        # 没有跨栏块：最简单的双栏阅读顺序就是"左栏从头到尾，再右栏从头到尾"
        return left + right, mode

    # 有跨栏块（比如页面中间插了一个通栏标题）：需要把左栏、右栏内容和跨栏块
    # 按纵向位置(y坐标)交替合并，而不是简单地"左栏全部+右栏全部+跨栏块"，
    # 否则跨栏标题下方本该紧跟的内容，阅读顺序上会被打乱到标题之前或太靠后
    ordered = []
    li = ri = 0  # 分别指向左栏、右栏"还没被消费"的下一个块
    for sp in spanning:
        sp_y = sp["bbox"][1]  # 这个跨栏块所在的纵向位置
        # 把左栏里"比这个跨栏块更靠上"（y更小）的块，都先按顺序放进结果里
        while li < len(left) and left[li]["bbox"][1] < sp_y:
            ordered.append(left[li])
            li += 1
        # 右栏同理
        while ri < len(right) and right[ri]["bbox"][1] < sp_y:
            ordered.append(right[ri])
            ri += 1
        ordered.append(sp)  # 该轮到这个跨栏块本身出场了
    # 循环结束后，把左右栏里剩下的（在最后一个跨栏块下方的）内容依次追加进去
    ordered.extend(left[li:])
    ordered.extend(right[ri:])
    return ordered, mode


def _finalize_native_page(raw_blocks: list[dict], width: float, page_no: int, force_layout: str) -> tuple[list[dict], str]:
    """把已剔除页眉页脚的原生页原始块，做完"分类block_type + 排出阅读顺序"，
    转换成和 _parse_pdf 最终期望的统一字典格式。
    """
    if not raw_blocks:
        return [], "single"  # 这一页剔除页眉页脚后什么都不剩，直接返回空结果
    # 用这一页所有块的平均字号的"中位数"当作"正文标准字号"（用中位数而不是平均数，
    # 是为了不被个别极端字号——比如一个超大标题——带偏）
    sizes = [b["avg_size"] for b in raw_blocks if b["avg_size"]]
    body_size = statistics.median(sizes) if sizes else 12.0
    for b in raw_blocks:
        b["block_type"] = _classify_native_block_type(b["avg_size"], body_size, b["zone"])

    ordered, mode = _order_native_page(raw_blocks, width, force_layout)
    # 转换成 _parse_pdf 期望的中间字典结构（跟B类OCR通道输出格式保持一致，方便后续合并处理）
    finalized = [
        {
            "text": b["text"],
            "block_type": b["block_type"],
            "source_location": _source_location(page_no, mode, b.get("column")),
            "confidence": None,  # 原生提取的文本没有OCR置信度这一说，固定填None
        }
        for b in ordered
    ]
    return finalized, mode
