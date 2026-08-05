"""A类（原生文字层PDF）分栏检测：分层占用率剖面。

对外只有 `_detect_column_boundaries(blocks, width) -> list[float]`（从左到右的分栏线
x 坐标，空列表=单栏，结果只可能是 0/1/3 条），签名与 A 类调用方 `native_pdf.py::
_order_native_page` 的既有契约一致。

**设计成坐标系无关**（分箱宽度按页宽比例而非绝对 pt），所以 A 类的 PDF 点（页宽约
1009）和 B 类的渲染像素（250 DPI 下约 3500）都能用；但目前只接 A 类，B 类仍走
`_common.py::_detect_column_split`——B 类的分栏结果只影响"左栏/右栏"这类位置描述措辞
（阅读顺序由 paddlex 的 XY-Cut 独立算出），换算法的收益纯属措辞精度，而 B 类一页只有
3~23 个块（A 类 90~110）、剖面稀疏，实测有页面从 1 栏变 2 栏且对错需逐页目视核，
收益与风险不对等。

## 唯一的几何信号：占用率，不看字符数

每个 x 分箱的值 = 覆盖该箱的块的 y 区间**并集长度** ÷ 参与统计的块的内容总高度，
占用率 ≤ `config.COLUMN_A_GAP_MAX_OCCUPANCY_RATIO`(τ) 的连续区间才算栏间距。

这是为了替掉两类不可靠信号：

- **二值覆盖图**（"这一竖条有没有块覆盖"）：两端对齐/行尾控制字符会让四五行溢出到
  栏间距里，二值图下这几行就把整条缝堵死。实测某期刊四栏页栏3右边界附近，溢出行让
  该处占用率只有 0.9%~7.3%，而栏内是 19%~21%——量"占了多高"能分开，量"有没有"不能。
- **字符数统计量**（分栏线两侧字符数平衡、非通栏块字符占比）：随页面内容剧烈波动，
  几何结构完全相同的 8 个页面检测结果是 2/2/4/2/4/2/4/2 这样随机跳变。

## 为什么必须分层，不能整页算一次剖面

占用率的分母是参与统计块的 y 并集高度。文字密集页约 660pt，通栏刊头高 9.4pt 只占
1.4%，"高度加权自动兜底、不需要剔通栏块"这个论断在这类页上成立；但**文章开篇页**
（大图+跨栏标题、正文少）内容高度只有 338~421pt，同一个跨栏大标题就占到 12.6%~13.6%，
直接盖过 τ 把真栏间距堵死。而 τ 又不能放高——放到 0.08 起，整页大表格的列间距会被
当成栏间距，正文被切碎（比漏检严重得多，见"安全方向"）。两头挤死。

出路是**堵缝的标题是局部现象，只有拿局部尺度去量才认得出来**：先在整页找主缝（跨页
对开刊物这条就是装订缝），再在左右两个半区里各用**该半区自己的内容范围宽度**重新剔
通栏块、重算剖面。同一个 397pt 宽的标题，在 1208pt 页宽下只占 33%（不算通栏、留在
统计里堵缝），在它实际所属的半区里就占 66%、该被剔除。

结构上是递归二分（主缝 + 两半各自细分 + 要求两半都成功才算四栏）；难点从来不在递归，
而在每一层用什么信号判断"这里是不是空白"。

## 安全方向：宁可判少，绝不凭空补线

缺一条真缝时降级到栏数更少的假设，**不做"按栏距外推补齐缺失边界"**。判少了最坏是维持
今天的交错行为；凭空补一条不存在的分栏线会把正文切碎，是唯一比"根本不检测分栏"更糟的失败模式。
已知代价：文章开篇页里标题只有约 1.5 栏宽、正好压在栏1|栏2 缝上的，仍判 2 栏。

## CV 只用于排序，不做否决

栏宽变异系数（`_width_cv`）在同一层的多个候选缝之间挑最均衡的那条，**从不否决任何
假设**。实测把上限从 0.05 放到 1.0，134 页 GT 命中数一个不变——分层结构里每层只选
一条缝，没有"多条缝的组合假设"需要被 CV 筛掉。真正起作用的是 τ、三个宽度下限、以及
"两半都要能再分"的结构约束。也因此不存在 `COLUMN_*_MAX_CV` 这个常量。

## 标定

工作点是 134 页人工目视核定的 GT 命中 129，另 221 页单栏文档零误判。每个常量的取值依据
与两侧余量写在 `config.py` 各常量上方，**改之前逐条读**——多个常量的安全窗只有一两档宽。

**过拟合风险要知道**：这几个阈值标定自同一家排版的 3 份期刊，τ 与
`COLUMN_A_SPANNING_WIDTH_RATIO` 的上界由同一页（唯一的整页大表格样本）决定。换出版社的
版面出问题时，保守退路是 `spanning_ratio` 放 0.7 + τ 降到 0.05（约 7 页四栏漏检，但离
"表格被拆栏"那个悬崖很远），别在原值附近小步试探。

核定方法留个提醒：**判栏数要目视（把分栏线叠到页面图上看），不要用块 x0 聚类代替**——
实测有一页左半页正文真的横跨栏1栏2（390pt 宽的项目符号行，占用率约 30% 不是噪声），
x0 聚类会把它当成栏边界，系统性高估栏数。
"""

from __future__ import annotations

import statistics

import config


def _union_length(intervals: list[tuple[float, float]]) -> float:
    """一组（可能重叠、**必须已按起点排序**）区间的并集总长度。

    占用率要用并集而不是长度求和：同一栏里上下相邻的行 bbox 常有 1~2pt 重叠，
    求和会把占用率算过头。
    """
    if not intervals:
        return 0.0
    total = 0.0
    cur_lo, cur_hi = intervals[0]
    for lo, hi in intervals[1:]:
        if lo > cur_hi:
            total += cur_hi - cur_lo
            cur_lo, cur_hi = lo, hi
        else:
            cur_hi = max(cur_hi, hi)
    return total + (cur_hi - cur_lo)


def _content_x_range(blocks: list[dict]) -> tuple[float, float] | None:
    """内容横向范围：全部非空块的 min(x0)/max(x1)，**不在这里剔通栏块**。

    通栏块的剔除只发生在"算剖面前过滤参与统计的块"那一步（`_exclude_spanning`），
    范围本身要按传进来的块如实取。语义与旧的 `_common.py::_content_extent` 正好相反，
    是有实测依据的：某期刊四栏页的栏4 是个只有 4 行的联系方式框，它整体占用率低于 τ，
    若再从范围里剔掉宽块，末栏宽度会算成负数。
    """
    xs = [(b["bbox"][0], b["bbox"][2]) for b in blocks if b.get("text", "").strip()]
    if not xs:
        return None
    return min(x0 for x0, _ in xs), max(x1 for _, x1 in xs)


def _occupancy_profile(
    blocks: list[dict], lo: float, hi: float, bin_width: float
) -> list[float]:
    """x 方向分箱，每箱 = 覆盖该箱的块的 y 并集长度 ÷ 这批块的内容总高度。"""
    n_bins = max(1, int(round((hi - lo) / bin_width)))
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(n_bins)]

    live = [b for b in blocks if b.get("text", "").strip()]
    content_height = _union_length(sorted((b["bbox"][1], b["bbox"][3]) for b in live))
    if content_height <= 0:
        return [0.0] * n_bins

    for b in live:
        x0, y0, x1, y1 = b["bbox"]
        i0 = max(0, min(n_bins - 1, int((x0 - lo) / bin_width)))
        i1 = max(0, min(n_bins - 1, int((x1 - lo) / bin_width)))
        for i in range(i0, i1 + 1):
            buckets[i].append((y0, y1))

    return [_union_length(sorted(iv)) / content_height for iv in buckets]


def _candidate_gaps(
    profile: list[float], lo: float, bin_width: float, min_gap_width: float
) -> list[tuple[float, float]]:
    """扫出全部占用率 ≤ τ 的连续区间，返回 [(x_start, x_end)]。

    丢掉贴着两端的（那是页边距不是栏间距）和宽度不足 min_gap_width 的。
    """
    tau = config.COLUMN_A_GAP_MAX_OCCUPANCY_RATIO
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for i, occ in enumerate(profile):
        if occ <= tau:
            if start is None:
                start = i
        elif start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(profile)))

    gaps = []
    for i0, i1 in runs:
        if i0 == 0 or i1 == len(profile):
            continue  # 贴边 = 页边距
        if (i1 - i0) * bin_width < min_gap_width:
            continue
        gaps.append((lo + i0 * bin_width, lo + i1 * bin_width))
    return gaps


def _column_widths(gaps: list[tuple[float, float]], lo: float, hi: float) -> list[float]:
    """由一组 gap 反推各栏宽度（栏 = 相邻 gap 之间的实体部分）。"""
    widths = []
    cursor = lo
    for g0, g1 in sorted(gaps):
        widths.append(g0 - cursor)
        cursor = g1
    widths.append(hi - cursor)
    return widths


def _width_cv(widths: list[float]) -> float:
    """栏宽变异系数 pstdev/mean，用于在同层候选缝之间挑最均衡的一条（不做否决）。

    用 CV 而不是方差（有量纲，跨页宽不可比）或极差（对栏数不敏感）：排版软件排多栏
    各栏等宽是硬约束，且完全不依赖内容多少。
    """
    if not widths:
        return float("inf")
    mean = statistics.fmean(widths)
    if mean <= 0:
        return float("inf")
    return statistics.pstdev(widths) / mean


def _exclude_spanning(blocks: list[dict], region_width: float) -> list[dict]:
    """剔掉宽度超过 region_width × `COLUMN_A_SPANNING_WIDTH_RATIO` 的块。

    **`region_width` 必须是调用方所在那一层的宽度，不能恒定用页宽**——这是分层结构
    存在的全部理由，见模块 docstring。
    """
    limit = region_width * config.COLUMN_A_SPANNING_WIDTH_RATIO
    return [b for b in blocks if (b["bbox"][2] - b["bbox"][0]) <= limit]


def _split_one_level(
    blocks: list[dict], page_width: float, min_gap_ratio: float
) -> float | None:
    """在这一层里找一条分割线，返回其 x 坐标；None 表示该区域不再细分。

    每层独立做三件事：按本层宽度剔通栏块 → 用剩余块的实际内容范围重算剖面 → 取候选缝。
    区域边界不用参数传——本层的尺度全部由 `blocks` 自己的内容范围现算（见下方注释），
    只有 `page_width` 是跨层不变的（分箱宽度与各阈值都按页宽比例定，才能坐标系无关）。
    """
    live = [b for b in blocks if b.get("text", "").strip()]
    # **通栏判定的尺子是本层的"内容范围宽度"，不是几何区域宽度**。几何半区是主缝到
    # 页边、含页边距；实测某期刊开篇页右半几何宽 604pt → 通栏门限 423pt，而堵住
    # 栏3|栏4 缝的文章大标题宽 397pt 就这么擦边留在统计里，把缝占到 0.13 盖过 τ，
    # 四栏退回两栏。改用右半内容范围宽 444pt → 门限 311pt，标题才被正确剔除。
    span_range = _content_x_range(live)
    if span_range is None:
        return None
    kept = _exclude_spanning(live, span_range[1] - span_range[0])
    if not kept:
        return None
    # 剖面范围取剩余块的 min/max：既满足"范围不能从剖面反推"（稀疏末栏整体低于 τ 会被
    # 截掉，末栏宽度算成负数），又能让跨页误聚的超宽块不再把半区范围撑回整页宽。
    content_range = _content_x_range(kept)
    if content_range is None:
        return None
    lo, hi = content_range
    bin_width = config.COLUMN_A_PROFILE_BIN_RATIO * page_width
    profile = _occupancy_profile(kept, lo, hi, bin_width)
    candidates = _candidate_gaps(profile, lo, bin_width, min_gap_ratio * page_width)

    min_column_width = config.COLUMN_A_MIN_WIDTH_RATIO * page_width
    best: tuple[float, float] | None = None      # (seam_x, cv)
    for gap in candidates:
        widths = _column_widths([gap], lo, hi)
        if min(widths) < min_column_width:
            continue
        cv = _width_cv(widths)
        if best is None or cv < best[1]:
            best = ((gap[0] + gap[1]) / 2, cv)
    return None if best is None else best[0]


def _detect_column_boundaries(blocks: list[dict], width: float) -> list[float]:
    """返回从左到右的全部分栏线 x 坐标，空列表表示单栏（只可能是 0/1/3 条）。

    **要求左右两半都能再分才承认四栏**：单边能分现实中多半是那半区排了张多列表格，
    按列输出会把表格行彻底打散，比原样按 y 排更糟。同理不往下递归第三层——真实版面
    没见过八栏。
    """
    main_seam = _split_one_level(blocks, width, config.COLUMN_A_MAIN_GAP_MIN_WIDTH_RATIO)
    if main_seam is None:
        return []

    sub_seams = []
    for lo, hi in ((0.0, main_seam), (main_seam, width)):
        half = [b for b in blocks if lo <= (b["bbox"][0] + b["bbox"][2]) / 2 < hi]
        seam = _split_one_level(half, width, config.COLUMN_A_SUB_GAP_MIN_WIDTH_RATIO)
        if seam is None:
            break
        sub_seams.append(seam)
    if len(sub_seams) != 2:
        return [main_seam]
    return sorted([main_seam, *sub_seams])
