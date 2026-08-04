"""B类(OCR)通道的分栏判定，以及 A/B 两条通道共用的位置描述拼装。

`_source_location` 是 A/B 共用的；`_find_extent_gap`/`_split_region`/
`_detect_column_split` 这套"二值覆盖图 + 字符数统计"的分栏判定**只服务 B 类**——A 类
已改用 `_columns.py` 的占用率剖面算法。两套并存不是疏漏：B 类的分栏结果只影响"左栏/
右栏"这类位置描述措辞（阅读顺序由 paddlex 的 XY-Cut 独立算出，不看分栏），换算法的收益
纯属措辞精度；而 B 类一页只有 3~23 个块（A 类 90~110），占用率剖面在这种稀疏度下的表现
需要逐页目视核对才敢用，收益与风险不对等。详见 `core/parser/_columns.py` 顶部 docstring。
"""

from __future__ import annotations

import config

_GAP_RESOLUTION = 200


def _find_extent_gap(
    extents: list[tuple[float, float]],
    lo: float,
    hi: float,
    band: tuple[float, float],
    min_gap_ratio: float,
) -> float | None:
    """在 extents（[(x0,x1), ...]）里找 [lo, hi] 区域中部的空白竖带，返回分栏线 x 坐标。

    思路：把区域宽度切成 _GAP_RESOLUTION(=200) 个小格子（"桶"），每个文本块
    覆盖到哪些格子就把对应格子标记为"有内容"；格子里从头到尾没被任何文本
    覆盖的，就是"空白"。只在区域中部 band（如 40%~60%）范围内找连续空白格，
    这段连续空白就是候选的分栏线位置；如果这段空白宽度太窄（占比低于
    min_gap_ratio），说明不是真正的栏间距，判定为没有分栏。

    区域用 [lo, hi] 而不是"从0到页宽"表示，是为了让同一套算法既能在整页上找
    主分栏线，又能在已分出的半区里找下一层子栏间距。
    """
    region_width = hi - lo
    if not extents or region_width <= 0:
        return None
    bin_width = region_width / _GAP_RESOLUTION          # 每个格子对应的实际宽度(pt/像素)
    covered = [False] * _GAP_RESOLUTION                 # 200个格子，标记是否被文本覆盖
    for x0, x1 in extents:
        i0 = max(0, min(_GAP_RESOLUTION - 1, int((x0 - lo) / bin_width)))  # 换算成"这是区域内第几个格子"，用max/min把区域外的部分夹回边界
        i1 = max(0, min(_GAP_RESOLUTION - 1, int((x1 - lo) / bin_width)))
        for i in range(i0, i1 + 1):                     # 标记所有被覆盖的格子
            covered[i] = True

    # 只在页面中部这个区间内寻找空白带（两端不算，比如页边距不该被当成分栏线）
    band_i0 = max(0, int(band[0] * _GAP_RESOLUTION))    # 计算band的起始位置对应的格子索引，即0.4*200
    band_i1 = min(_GAP_RESOLUTION, int(band[1] * _GAP_RESOLUTION))  # 计算band的结束位置对应的格子索引，即0.6*200

    # 在 band 范围内扫描，找出最长的一段连续"未被覆盖"（即空白）的格子区间
    best: tuple[int, int] | None = None
    start: int | None = None
    for i in range(band_i0, band_i1):                   # 遍历 band 范围内的格子索引，如果是空格子且没有start，就记录start为当前索引
        if not covered[i]:                              # 如果是有内容的格子且start为None，说明现在还无法确定空白格子的开头，一直跳过，直到遇到空格子为止
            if start is None:
                start = i
        elif start is not None:                         # 如果遇到有内容的格子且start不为None，说明空白区间结束了，就和best比较长度，更新best
            if best is None or (i - start) > (best[1] - best[0]):
                best = (start, i)
            start = None
    if start is not None:
        # 循环结束时空白区间还没闭合（一直空白到band边界），也要参与比较
        if best is None or (band_i1 - start) > (best[1] - best[0]):
            best = (start, band_i1)

    if best is None:
        return None  # band范围内完全没有空白，不存在分栏
    gap_width = (best[1] - best[0]) * bin_width
    if gap_width / region_width < min_gap_ratio:
        return None  # 空白带太窄，可能只是普通字间距，不采信为分栏线
    return lo + (best[0] + best[1]) / 2 * bin_width  # 返回空白带中点作为分栏线的x坐标


def _split_region(blocks: list[dict], lo: float, hi: float, min_gap_ratio: float) -> float | None:
    """在 [lo, hi] 区域内找一条有效分栏线，找不到返回 None。

    三道关卡，缺一不可：
    1. 剔除通栏块（宽度超过区域宽度 config.COLUMN_SPANNING_BLOCK_WIDTH_RATIO 的块）
       后再画覆盖图——通栏刊头/标题会把整条栏间空白带盖住，只要有一个就足以让
       明显的分栏版面被误判成单栏。
    2. 剩余块的字符数要占本区域总字符数 config.COLUMN_NON_SPANNING_MIN_CHAR_RATIO
       以上——单栏页每行本身就接近整幅宽度会被全部剔除，没有这道闸门，剔除后
       近乎空白的覆盖图会让任何单栏页都"找得到"空白带。
    3. 分栏线两侧字符数要大致平衡（config.COLUMN_BALANCE_MIN_RATIO）——真正的
       分栏内容大致对半分布，孤立块造成的伪分栏线两侧字符数会严重失衡。
       只按剩余块统计，通栏块不该记到任何一侧头上。
    """
    region_width = hi - lo
    if region_width <= 0 or not blocks:
        return None
    total_chars = sum(len(b["text"]) for b in blocks)
    if total_chars == 0:
        return None

    max_block_width = region_width * config.COLUMN_SPANNING_BLOCK_WIDTH_RATIO
    in_column = [b for b in blocks if (b["bbox"][2] - b["bbox"][0]) <= max_block_width]
    in_column_chars = sum(len(b["text"]) for b in in_column)
    if in_column_chars / total_chars < config.COLUMN_NON_SPANNING_MIN_CHAR_RATIO:
        return None

    extents = [(b["bbox"][0], b["bbox"][2]) for b in in_column]
    split_x = _find_extent_gap(extents, lo, hi, config.COLUMN_GAP_BAND, min_gap_ratio)
    if split_x is None:
        return None  # 中部找不到有效空白带，说明这个区域不再细分
    left_chars = sum(len(b["text"]) for b in in_column if (b["bbox"][0] + b["bbox"][2]) / 2 < split_x)
    left_ratio = left_chars / in_column_chars
    if not (config.COLUMN_BALANCE_MIN_RATIO <= left_ratio <= 1 - config.COLUMN_BALANCE_MIN_RATIO):
        return None  # 左右字符数严重失衡（如左侧只占5%），说明这不是真正的分栏，只是偶然的空白
    return split_x


def _detect_column_split(blocks: list[dict], width: float) -> float | None:
    """判断整页是否存在有效分栏，返回主分栏线 x 坐标（无分栏返回 None）。

    唯一调用点是 B 类的 `ocr_pdf.py::_ocr_and_order`——它只需要"分不分栏"这一个布尔
    意义上的答案加一条分界线，不需要全部栏边界（A 类要全部边界，走 `_columns.py`）。
    """
    return _split_region(blocks, 0.0, width, config.COLUMN_MIN_GAP_WIDTH_RATIO)


def _source_location(page_no: int, mode: str, column: str | None) -> str:
    """拼出人类可读的位置描述，写进 ParsedBlock.source_location。

    column 取 'left'/'right'（两栏版面）、'col1'..'colN'（三栏以上，此时不用
    左/右描述，直接报第几栏）、'span'（跨栏）或 None（单栏）。

    页码一律用PDF物理页码，不分单栏双栏、也不看该页有没有提取到期刊自身页码：
    期刊页码的提取本身不可靠（目录/封面等页提取不到，对开版面会提取到两个），
    位置描述里混着两种页码体系反而更难核对。校对时是对着PDF翻页找问题，PDF页码
    是唯一始终存在且始终准确的坐标。提取到的期刊页码仍单独保留在
    `ParsedBlock.doc_page`，走 Excel"文档页码"列作为补充信息。
    """
    if mode != "double" or column is None:
        return f"第{page_no}页"  # 单栏，或没有栏位信息，只报页码
    if column == "left":
        return f"第{page_no}页左栏"
    if column == "right":
        return f"第{page_no}页右栏"
    if column.startswith("col") and column[3:].isdigit():
        return f"第{page_no}页第{column[3:]}栏"
    return f"第{page_no}页通栏"  # 分栏页面里跨越多栏的内容（如通栏标题/表格）
