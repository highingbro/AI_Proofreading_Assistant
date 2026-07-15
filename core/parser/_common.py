"""A类(原生PDF)与B类(OCR)通道共用的坐标/分栏判定工具。

从 core/parser.py 拆分而来（原逻辑未改动）：两条通道各自的分块坐标结构
字段一致（都有 "bbox"/"text"），共用同一套"找空白分栏带"算法。
"""

from __future__ import annotations

import config

_GAP_RESOLUTION = 200


def _find_extent_gap(
    extents: list[tuple[float, float]],
    total_width: float,
    band: tuple[float, float],
    min_gap_ratio: float,
) -> float | None:
    """在 extents（[(x0,x1), ...]）里找页面中部空白竖带，返回分栏线 x 坐标。

    思路：把页面宽度切成 _GAP_RESOLUTION(=200) 个小格子（"桶"），每个文本块
    覆盖到哪些格子就把对应格子标记为"有内容"；格子里从头到尾没被任何文本
    覆盖的，就是"空白"。只在页面中部 band（如 40%~60%）范围内找连续空白格，
    这段连续空白就是候选的分栏线位置；如果这段空白宽度太窄（占比低于
    min_gap_ratio），说明不是真正的栏间距，判定为没有分栏。
    """
    if not extents or total_width <= 0:
        return None
    bin_width = total_width / _GAP_RESOLUTION           # 每个格子对应的实际宽度(pt/像素)
    covered = [False] * _GAP_RESOLUTION                 # 200个格子，标记是否被文本覆盖
    for x0, x1 in extents:
        i0 = max(0, min(_GAP_RESOLUTION - 1, int(x0 / bin_width)))  # int(x0 / bin_width)计算这是第几个格子，然后用max和min确保不会越界
        i1 = max(0, min(_GAP_RESOLUTION - 1, int(x1 / bin_width)))
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
    if gap_width / total_width < min_gap_ratio:
        return None  # 空白带太窄，可能只是普通字间距，不采信为分栏线
    return (best[0] + best[1]) / 2 * bin_width  # 返回空白带中点作为分栏线的x坐标


def _detect_column_split(blocks: list[dict], width: float) -> float | None:
    """判断是否存在有效分栏。

    先找候选分栏空白带，再要求两侧文本字符数都达到总字符数一定比例才采信
    ——真正的双栏内容大致对半分布，孤立块造成的伪分栏线两侧字符数会严重
    失衡。不按块宽度筛"宽块"：同一版面解析模型对单栏/双栏文本的分块粒度
    本身就不稳定，窄块在两种情况下都很常见，按宽度过滤并不可靠。

    A类(PyMuPDF坐标)和B类(OCR版面区域)共用本函数：两边的block字典都带
    "bbox"/"text"字段，几何判定逻辑完全通用。
    """
    extents = [(b["bbox"][0], b["bbox"][2]) for b in blocks]
    split_x = _find_extent_gap(extents, width, config.COLUMN_GAP_BAND, config.COLUMN_MIN_GAP_WIDTH_RATIO)
    if split_x is None:
        return None  # 中部找不到有效空白带，说明是单栏
    # 再核实"两侧字符数是否大致平衡"：按每个块中心点在分栏线左边还是右边，统计左侧字符占比
    total_chars = sum(len(b["text"]) for b in blocks)
    if total_chars == 0:
        return None
    left_chars = sum(len(b["text"]) for b in blocks if (b["bbox"][0] + b["bbox"][2]) / 2 < split_x)
    left_ratio = left_chars / total_chars
    if not (config.COLUMN_BALANCE_MIN_RATIO <= left_ratio <= 1 - config.COLUMN_BALANCE_MIN_RATIO):
        return None  # 左右字符数严重失衡（如左侧只占5%），说明这不是真正的双栏，只是偶然的空白
    return split_x


def _source_location(page_no: int, mode: str, column: str | None) -> str:
    """拼出人类可读的位置描述，写进 ParsedBlock.source_location。"""
    if mode != "double" or column is None:
        return f"第{page_no}页"  # 单栏，或没有栏位信息，只报页码
    if column == "left":
        return f"第{page_no}页左栏"
    if column == "right":
        return f"第{page_no}页右栏"
    return f"第{page_no}页通栏"  # 双栏页面里跨越左右两栏的内容（如通栏标题/表格）
