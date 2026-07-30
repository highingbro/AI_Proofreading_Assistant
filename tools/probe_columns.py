"""分栏检测只读探针：看每一页的占用率分布长什么样，以及栏数对各阈值有多敏感。

**新文档分栏出问题，第一步跑它、不要直接调 `config.COLUMN_A_*`**——那些阈值标定自同一家
排版的 3 份期刊，两侧余量很窄（见 `tools/column_probe_data.md`），拍脑袋放大很容易在别处
坏掉。算法本身的设计理由在 `core/parser/_columns.py` 顶部 docstring。

- **阈值全部走命令行参数**，默认值与 `config.py` 的定稿值一致；`--sweep-tau` 一次调用看
  多个 τ 下的栏数，不用改代码重跑。生产实现只返回分栏线坐标，拿不到这些中间量，所以本
  脚本镜像了一份可调参版本——**有漂移风险，改生产算法后要按下方注释里的命令核对一致性**。
- **`--render` 把新旧分栏线叠加到页面图上目视核对**。不要用 x0 聚类代替目视：实测活动计划
  第8页左半页正文真的横跨栏1栏2（390pt 宽的项目符号行，占用率约30%不是噪声），x0 聚类会
  把它错标成四栏，系统性高估栏数。
- **页号统一 0-indexed 并在输出里标注**：既有诊断数据里 0/1-indexed 混用过，吃过亏。
- **A/B 两条通道都能探**（`--channel`）：A类一个 block 是 PyMuPDF 的一个文本行，B类一个
  block 是版面检测模型输出的整个区域，块颗粒度差一个数量级。实测结论是 B 类不换算法
  （原因见 `core/parser/_columns.py` docstring），但探 B 类的能力保留着，将来要重新评估
  时不用重写。

用法：

    # A类（有文字层PDF），最常用
    python -X utf8 tools/probe_columns.py "samples/预览版 8-9月 电子版-2026e-works活动计划-7月更新v1.pdf"

    # 只看某几页 + 打印分箱占用率数组
    python -X utf8 tools/probe_columns.py <pdf> --pages 8-15 --dump-profile

    # τ 敏感性（标定 COLUMN_GAP_MAX_OCCUPANCY_RATIO 用）
    python -X utf8 tools/probe_columns.py <pdf> --sweep-tau

    # B类（无文字层扫描件，要跑OCR，慢）——见下方"Windows 编码"注意事项
    python -X utf8 tools/probe_columns.py samples/系统管理员.pdf --channel b

**Windows 编码**：`--channel b` 会初始化 PaddleOCR，它有个子进程按系统代码页输出，
重定向到文件会混合编码。要重定向时先在 **PowerShell**（不是 Git Bash，那里 `chcp`
静默失效）执行 `chcp 65001`，并且始终带 `-X utf8`。细节见 core/parser/CLAUDE.md。
"""

from __future__ import annotations

import argparse
import statistics
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core.parser._common import _split_region  # noqa: E402  切换前那套算法唯一还在的零件


def _detect_column_boundaries_legacy(blocks: list[dict], width: float) -> list[float]:
    """**切换前那套算法的冻结副本**，只用来做并排对比，不是生产代码。

    A 类已改用 `core/parser/_columns.py`；这套"二值覆盖图 + 字符数统计"的四栏递归随之
    从 `_common.py` 删掉了（只剩 `_split_region` 一族继续服务 B 类）。这里留一份副本，
    是为了让 `column_probe_data.md` 里记的"收益41/回归0"能一直复算得出来——基线按定义
    就该是冻结的，不需要跟着生产代码走，所以复制在这里不存在"验证复制品"的问题
    （`compare_columns.py` 打桩去调真正的 `_order_native_page` 是另一回事，那边比的是
    排序行为，必须用生产代码）。
    """
    top = _split_region(blocks, 0.0, width, config.COLUMN_MIN_GAP_WIDTH_RATIO)
    if top is None:
        return []
    sub_splits = []
    for lo, hi in ((0.0, top), (top, width)):
        half = [b for b in blocks if lo <= (b["bbox"][0] + b["bbox"][2]) / 2 < hi]
        # 半区内容范围也要按同一把尺子剔通栏块，否则跨页误聚的超宽块会把范围撑回整页宽
        max_w = (hi - lo) * config.COLUMN_SPANNING_BLOCK_WIDTH_RATIO
        in_column = [b for b in half if (b["bbox"][2] - b["bbox"][0]) <= max_w]
        if not in_column:
            break
        extent = (min(b["bbox"][0] for b in in_column), max(b["bbox"][2] for b in in_column))
        # 子缝下限 0.03：切换前是 config.COLUMN_SUB_GAP_MIN_WIDTH_RATIO，因为生产侧再没有
        # 调用者、已从 config.py 删掉，写死在基线里（基线本就该是冻结值，不跟着配置走）
        sub = _split_region(half, extent[0], extent[1], 0.03)
        if sub is None:
            break
        sub_splits.append(sub)
    if len(sub_splits) != 2:
        return [top]
    return sorted([top, *sub_splits])

# ---------------------------------------------------------------------------
# 生产算法的**可调参数版镜像**（生产实现在 core/parser/_columns.py）
#
# 为什么要镜像一份而不是直接调生产函数：生产函数只返回 list[float]，而探针的全部价值
# 在于中间量——`--dump-profile` 要占用率数组、`--levels` 要每层的范围/候选/栏宽、
# `--sweep-tau` 要一次调用换一组阈值。这些都得把内部打开才拿得到。
#
# ⚠️ **代价是会漂移**：改了 `_columns.py` 的判据却没同步这里，探针的诊断结论就会误导人。
# 改完两边跑这一行确认仍然逐页一致（应输出 0 页不一致）：
#
#   python -X utf8 -c "import sys;sys.path.insert(0,'tools');from pathlib import Path;\
#   import probe_columns as P;from core.parser._columns import _detect_column_boundaries as N;\
#   pg=P.load_native_pages(Path('samples/数字化企业期刊82期V6.25.pdf'));\
#   print(sum(P.detect(b,w,{**P.DEFAULTS,'flat':False})['n_cols']!=len(N(b,w))+1 for b,w in pg),'页不一致')"
#
# 与被替换的老算法的根本区别：只用几何量（被文字覆盖的y长度占比），完全不看字符数。
# 老算法的 COLUMN_BALANCE_MIN_RATIO / COLUMN_NON_SPANNING_MIN_CHAR_RATIO 都是内容
# 统计量，随页面内容剧烈波动，同样版面的页结果会跳变（实测第8~15页 2/2/4/2/4/2/4/2）。
# ---------------------------------------------------------------------------

# 与 config.py 的 COLUMN_A_* 定稿值一致。两侧实测余量见 tools/column_probe_data.md 第七节；
# 工作点 = 134 页 GT 命中 129、收益 41、回归 0，另有 221 页单栏零误判。
DEFAULTS = {
    "bin_ratio": 0.002,        # 0.001~0.003 同为最优；0.004 起掉点，0.008 崩
    "tau": 0.06,              # 安全区 0.06~0.075；0.05 少3页，**0.08 起整页表格被拆栏(R1)**
    "gap_min_width": 0.005,    # 子缝下限。0.002~0.005 同为最优；0.015 起回归
    "two_col_min_gap": 0.05,   # 主缝下限。0.02~0.05 同为最优；0.10 起回归
    "min_col_width": 0.10,     # 0.05~0.12 同为最优；0.13 起回归
    "spanning_ratio": 0.6,     # **最脆的一个**：安全窗仅 0.58~0.60，两侧都会坏（见数据文件）
    # 下面两个只被已证伪的 detect_flat 用到，分层实现里完全不参与判断，不进 config.py
    "max_cv": 0.15,
    "max_candidates": 6,
}


# ---------------------------------------------------------------------------
# Ground truth（目视核定，页号 0-indexed）
#
# 核定方法：--render 把新旧分栏线叠加到页面图上，直接看每条线是否落在真栏间距里；
# 四栏页再用"跨栏续写句子是否连贯"交叉验证。**不用 x0 聚类代替目视**——实测活动计划
# 第8页左半页正文真的横跨栏1栏2（390pt宽项目符号行，占用率约30%不是噪声）。
#
# 这三份期刊都是跨页对开装订（一个物理页印两个页码，如 21|22），所以"四栏"=左右两页各两栏。
# 只收录真正目视核过的页；没核过的页不写进来，不靠推测填表。
# ---------------------------------------------------------------------------

GROUND_TRUTH: dict[str, dict[int, int]] = {
    "预览版": {0: 2, 11: 4, 13: 4, 15: 4},
    "79期": {
        0: 2, 2: 2, 3: 4, 4: 4, 5: 4, 6: 4, 8: 4, 9: 4, 11: 4, 13: 4, 14: 4, 15: 4,
        16: 4, 19: 2, 21: 4, 22: 4, 23: 4, 24: 4, 25: 4, 26: 4, 29: 4, 30: 4, 31: 4,
        33: 4, 34: 4, 35: 4,
    },
    "82期": {
        0: 2, 3: 4, 6: 4, 7: 4, 8: 4, 9: 4, 10: 4, 11: 4, 12: 4, 13: 4, 14: 4, 15: 4,
        16: 4, 17: 2, 18: 4, 22: 4, 23: 4, 24: 4, 25: 4, 26: 4, 28: 4,
    },
    # 单栏文档：全页 1 栏，逐份核过零候选 gap
    "sample.pdf": dict.fromkeys(range(4), 1),
    "学生": dict.fromkeys(range(38), 1),
    "后台管理员": dict.fromkeys(range(41), 1),
}


def _gt_for(path: Path) -> dict[int, int]:
    for key, table in GROUND_TRUTH.items():
        if key in path.name:
            return table
    return {}


def _union_length(intervals: list[tuple[float, float]]) -> float:
    """一组可能重叠的区间的并集总长度。

    占用率必须用并集而不是"块个数"或"长度求和"：同一栏里上下相邻的行 bbox 常有
    1~2pt 重叠，求和会把占用率算过头；而块个数完全丢失了"占了多高"这个关键信息，
    正是旧算法二值覆盖图的毛病——一行溢出到栏间距就把整条 gap 判死。
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
    """内容横向范围：全部非空块的 min(x0)/max(x1)，**不剔除通栏块**。

    第16页的栏4 是个只有 4 行的联系方式框，按占用率它整体低于 τ，若再从范围里剔掉
    宽块，末栏宽度会算成负数。剔通栏块只发生在"算剖面前过滤参与统计的块"那一步
    （`_exclude_spanning`），而且必须按当前层的尺度剔。
    """
    xs = [(b["bbox"][0], b["bbox"][2]) for b in blocks if b.get("text", "").strip()]
    if not xs:
        return None
    return min(x0 for x0, _ in xs), max(x1 for _, x1 in xs)


def _occupancy_profile(blocks: list[dict], lo: float, hi: float, bin_width: float) -> list[float]:
    """x 方向分箱，每箱的值 = 覆盖该箱的块的 y 区间并集长度 ÷ 全页内容高度。"""
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
    profile: list[float], lo: float, bin_width: float, tau: float, min_gap_width: float
) -> list[tuple[float, float, float]]:
    """扫出全部 occ <= tau 的连续区间，返回 [(x_start, x_end, 区间内最大占用率)]。

    丢掉贴着两端的（那是页边距不是栏间距）和宽度不足 min_gap_width 的。
    """
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
        peak = max(profile[i0:i1]) if i1 > i0 else 0.0
        gaps.append((lo + i0 * bin_width, lo + i1 * bin_width, peak))
    return gaps


def _column_widths(gaps: list[tuple[float, float, float]], lo: float, hi: float) -> list[float]:
    """由一组 gap 反推各栏宽度（栏 = 相邻 gap 之间的实体部分）。"""
    widths = []
    cursor = lo
    for g0, g1, _ in sorted(gaps):
        widths.append(g0 - cursor)
        cursor = g1
    widths.append(hi - cursor)
    return widths


def _width_cv(widths: list[float]) -> float:
    """栏宽变异系数 pstdev/mean。

    用 CV 而不是方差（有量纲，跨页宽不可比）或极差（对栏数不敏感）：排版软件排多栏
    各栏等宽是硬约束，且完全不依赖内容多少，正是内容统计量所缺的稳定性。
    """
    if not widths:
        return float("inf")
    mean = statistics.fmean(widths)
    if mean <= 0:
        return float("inf")
    return statistics.pstdev(widths) / mean


def _select_columns(
    gaps: list[tuple[float, float, float]], lo: float, hi: float, width: float, cfg: dict
) -> tuple[list[tuple[float, float, float]], float | None]:
    """子集枚举定栏数，返回 (选中的gap子集, 该假设的CV)；空列表表示单栏。

    **先试栏数多的**是有意的：四栏页上"三条线"和"仅主栏线"两个假设 CV 都很小，
    必须优先取信息更多的那个，否则又退回两栏。缺一条真 gap 时自然降级到栏数更少的
    假设——安全方向，按更少的栏读最坏是交错，而"凭空补一条不存在的分栏线"会把正文
    切碎，所以 v1 明确不做按栏距外推补齐。
    """
    candidates = sorted(gaps, key=lambda g: g[1] - g[0], reverse=True)[: cfg["max_candidates"]]
    min_col_w = cfg["min_col_width"] * width

    for k in range(min(cfg["max_columns"] - 1, len(candidates)), 0, -1):
        best: tuple[list, float] | None = None
        for sub in combinations(candidates, k):
            widths = _column_widths(list(sub), lo, hi)
            if min(widths) < min_col_w:
                continue
            if k >= 2:
                cv = _width_cv(widths)
                if cv <= cfg["max_cv"] and (best is None or cv < best[1]):
                    best = (sorted(sub), cv)
            else:
                # k==1 走独立判据，**不能用CV否决两栏**：实测第7页只有一个gap，
                # 两侧栏宽 214 vs 461（正文+窄边栏，真实存在的版面），CV=0.36 会被
                # 否决成单栏，比旧算法还差。
                g0, g1, _ = sub[0]
                if (g1 - g0) >= cfg["two_col_min_gap"] * width:
                    best = (sorted(sub), _width_cv(widths))
        if best is not None:
            return best
    return [], None


def detect_flat(blocks: list[dict], width: float, cfg: dict) -> dict:
    """扁平变体：整页算一次剖面 + 组合枚举定栏数。

    **已被实测证伪，只留作对比基线**：整页一个剖面意味着占用率的分母是全页文字高度，
    而"文章开篇页"（大图+跨栏标题、正文少）分母只有 338~421pt，一个跨栏大标题就占到
    12.6%~13.6%，盖过 τ 把真栏间距堵死；同时 τ 又不能放高，否则整页表格被拆栏。
    两头挤死无解，所以生产实现走 detect_layered。
    """
    rng = _content_x_range(blocks)
    if rng is None:
        return {"n_cols": 1, "gaps": [], "candidates": [], "range": None, "cv": None,
                "profile": [], "widths": []}
    lo, hi = rng
    bin_width = cfg["bin_ratio"] * width
    profile = _occupancy_profile(blocks, lo, hi, bin_width)
    candidates = _candidate_gaps(profile, lo, bin_width, cfg["tau"], cfg["gap_min_width"] * width)
    chosen, cv = _select_columns(candidates, lo, hi, width, cfg)
    return {
        "n_cols": len(chosen) + 1,
        "gaps": chosen,
        "candidates": candidates,
        "range": (lo, hi),
        "cv": cv,
        "profile": profile,
        "widths": _column_widths(chosen, lo, hi),
    }


def _exclude_spanning(blocks: list[dict], region_width: float, cfg: dict) -> list[dict]:
    """剔掉宽度超过本区域 spanning_ratio 的块。

    **必须按"当前层的区域宽度"算，不能恒定用页宽**——这是分层结构存在的全部理由。
    一个 397pt 宽的文章大标题放在 1208pt 页宽下只占 33%（不算通栏、留在统计里堵死栏间距），
    但放在它实际所属的 604pt 半区里就占 66%，接近通栏、该被剔除。局部现象只有拿局部尺度
    才量得出来。
    """
    limit = region_width * cfg["spanning_ratio"]
    return [b for b in blocks if (b["bbox"][2] - b["bbox"][0]) <= limit]


def _split_one_level(blocks: list[dict], lo_hint: float, hi_hint: float,
                     page_width: float, cfg: dict, min_gap_ratio: float) -> dict:
    """在 [lo_hint, hi_hint] 这一层里找一条分割线。返回诊断字典，`seam` 为 None 表示不再细分。

    每层独立做三件事：按本层宽度剔通栏块 → 用剩余块的实际内容范围重算剖面 → 取候选缝。
    """
    empty = {"seam": None, "candidates": [], "range": None, "cv": None, "profile": [], "widths": []}
    if hi_hint - lo_hint <= 0:
        return empty
    live = [b for b in blocks if b.get("text", "").strip()]
    # **通栏判定的尺子必须是本层的"内容范围宽度"，不是几何区域宽度**。几何半区是
    # 主缝到页边，含页边距；实测82期p11：右半几何宽 604pt → 通栏门限 423pt，而堵住
    # 栏3|栏4 缝的文章大标题宽 397pt，就这么擦边留在了统计里、把缝堵死（占用率0.13
    # 盖过 τ），四栏退回两栏。改用右半内容范围宽 444pt → 门限 311pt，397pt 的标题
    # 才被正确认定为"这半区里的通栏块"。
    span_rng = _content_x_range(live)
    if span_rng is None:
        return empty
    kept = _exclude_spanning(live, span_rng[1] - span_rng[0], cfg)
    if not kept:
        return empty
    # 剩余块的 min/max 作剖面范围：既满足"不能从剖面反推"（稀疏末栏会被截掉，见坑1），
    # 又能让跨页误聚的超宽块不再把半区范围撑回整页宽。
    rng = _content_x_range(kept)
    if rng is None:
        return empty
    lo, hi = rng
    bin_width = cfg["bin_ratio"] * page_width
    profile = _occupancy_profile(kept, lo, hi, bin_width)
    candidates = _candidate_gaps(profile, lo, bin_width, cfg["tau"], min_gap_ratio * page_width)

    min_col_w = cfg["min_col_width"] * page_width
    best = None
    for g in candidates:
        widths = _column_widths([g], lo, hi)
        if min(widths) < min_col_w:
            continue
        cv = _width_cv(widths)
        if best is None or cv < best[1]:
            best = (g, cv, widths)
    if best is None:
        return {**empty, "candidates": candidates, "range": (lo, hi), "profile": profile}
    g, cv, widths = best
    return {"seam": (g[0] + g[1]) / 2, "seam_gap": g, "candidates": candidates,
            "range": (lo, hi), "cv": cv, "profile": profile, "widths": widths}


def detect_layered(blocks: list[dict], width: float, cfg: dict) -> dict:
    """分层变体（生产实现的目标形态）：先找主缝，再在左右两半各自的尺度上找子缝。

    结构上与被替换的旧递归二分相同（主缝 + 两半各自细分 + 要求两半都成功才算四栏），
    换掉的是每一层的判据：占用率剖面 + 栏宽CV，而不是二值覆盖图 + 字符数平衡。
    """
    top = _split_one_level(blocks, 0.0, width, width, cfg, cfg["two_col_min_gap"])
    if top["seam"] is None:
        return {"n_cols": 1, "gaps": [], "candidates": top["candidates"],
                "range": top["range"], "cv": None, "profile": top["profile"],
                "widths": [], "levels": [top]}

    seam = top["seam"]
    subs, sub_seams = [], []
    for lo, hi in ((0.0, seam), (seam, width)):
        half = [b for b in blocks if lo <= (b["bbox"][0] + b["bbox"][2]) / 2 < hi]
        r = _split_one_level(half, lo, hi, width, cfg, cfg["gap_min_width"])
        subs.append(r)
        if r["seam"] is not None:
            sub_seams.append(r["seam"])

    # 要求两半都能再分才承认四栏：单边能分现实中多半是那半区排了张多列表格，
    # 按列输出会把表格行彻底打散，比原样按y排更糟。缺一条真缝时降级到两栏是安全方向。
    four = len(sub_seams) == 2
    accepted = [top, *subs] if four else [top]
    chosen_gaps = [r["seam_gap"] for r in accepted if r["seam"] is not None]
    lines = sorted(r["seam"] for r in accepted if r["seam"] is not None)
    lo, hi = top["range"]
    return {
        "n_cols": len(lines) + 1,
        "gaps": chosen_gaps,
        # 候选汇总各层，渲染时能看到子层被否决的候选（黄带），不只是顶层的
        "candidates": [g for r in [top, *subs] for g in r["candidates"]],
        "range": (lo, hi),
        "cv": top["cv"],
        "profile": top["profile"],
        "widths": _column_widths([(x, x, 0.0) for x in lines], lo, hi),
        "levels": [top, *subs],
        "sub_split": [r["seam"] is not None for r in subs],
    }


def detect(blocks: list[dict], width: float, cfg: dict) -> dict:
    return (detect_flat if cfg.get("flat") else detect_layered)(blocks, width, cfg)


# ---------------------------------------------------------------------------
# 取块：A类 / B类两条通道
# ---------------------------------------------------------------------------

def load_native_pages(path: Path) -> list[tuple[list[dict], float]]:
    """A类：PyMuPDF 文本行，走生产同一条路径（提取 → 跨页剔页眉页脚）。"""
    import fitz

    from core.parser.native_pdf import _extract_native_page_raw, _strip_headers_footers

    doc = fitz.open(str(path))
    raws, widths = [], []
    for i in range(doc.page_count):
        page = doc[i]
        blocks, _h = _extract_native_page_raw(page)
        raws.append(blocks)
        widths.append(page.rect.width)
    bodies, _doc_pages = _strip_headers_footers(raws)
    return list(zip(bodies, widths))


def load_ocr_pages(path: Path, wanted: set[int] | None = None) -> list[tuple[list[dict], float] | None]:
    """B类：版面检测区域。只取 paragraph/heading，与生产 `_ocr_and_order` 的
    `text_like` 过滤保持一致，否则表格/图注坐标会干扰对比。

    `wanted` 之外的页返回 None 占位、不跑OCR——B类一页要跑版面检测+文字识别两个
    模型，几秒起步，排查单页问题时没必要把整份文档重跑一遍（A类是纯坐标提取、
    毫秒级，且 `_strip_headers_footers` 必须跨全部页比较，所以不做这个优化）。
    """
    import fitz

    import config
    from core.parser.ocr_pdf import _render_page_image, _run_structure

    doc = fitz.open(str(path))
    out: list[tuple[list[dict], float] | None] = []
    for i in range(doc.page_count):
        if wanted is not None and i not in wanted:
            out.append(None)
            continue
        img = _render_page_image(doc[i], config.OCR_RENDER_DPI)
        raw_blocks, _conf, _dp = _run_structure(img)
        text_like = [b for b in raw_blocks if b["block_type"] in ("paragraph", "heading")]
        out.append((text_like, float(img.width)))
    return out


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def render_overlay(path: Path, page_index: int, probe_width: float, res: dict,
                   old: list[float], out_path: Path, dpi: int = 100) -> None:
    """把页面渲染成图，并把新旧算法的分栏线叠加上去，供目视核定真实栏数。

    **必须目视、不能靠 x0 聚类代替**：实测活动计划第8页左半页的正文是真的横跨栏1栏2
    （多行 390pt 宽的项目符号行，占用率约30%不是噪声），x0 聚类会把它错标成四栏，
    系统性高估栏数。叠加图让"这条线是不是落在真的栏间距里"变成能直接看出来的事。

    绿色=新算法选中的gap（半透明带+中心线），黄色=被枚举否决的候选gap，
    红色虚线=旧算法分栏线，蓝色=内容x范围边界。
    """
    import fitz
    from PIL import Image, ImageDraw

    page = fitz.open(str(path))[page_index]
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), colorspace=fitz.csRGB)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("RGBA")
    # 探针坐标(A类是PDF点/B类是OCR渲染像素)换算到本次渲染的像素尺度
    scale = pix.width / probe_width
    H = img.height

    band = Image.new("RGBA", img.size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    chosen = set(res["gaps"])
    for g0, g1, _peak in res["candidates"]:
        color = (0, 200, 0, 70) if (g0, g1, _peak) in chosen else (255, 200, 0, 60)
        bd.rectangle([g0 * scale, 0, g1 * scale, H], fill=color)
    img = Image.alpha_composite(img, band)

    d = ImageDraw.Draw(img)
    for g0, g1, _ in res["gaps"]:
        cx = (g0 + g1) / 2 * scale
        d.line([cx, 0, cx, H], fill=(0, 160, 0), width=3)
    for x in old:                                    # 旧算法：红色虚线
        for y in range(0, H, 24):
            d.line([x * scale, y, x * scale, y + 12], fill=(220, 0, 0), width=3)
    if res["range"]:
        for x in res["range"]:
            d.line([x * scale, 0, x * scale, H], fill=(0, 80, 255), width=1)

    label = f"p{page_index}  old={len(old) + 1}  new={res['n_cols']}"
    d.rectangle([0, 0, 8 + 7 * len(label), 22], fill=(255, 255, 255))
    d.text((4, 5), label, fill=(0, 0, 0))
    img.convert("RGB").save(out_path)


def make_montage(paths: list[Path], out_path: Path, cols: int = 3) -> None:
    """把多张页面图拼成一张，便于一次看完一批差异页。"""
    from PIL import Image

    imgs = [Image.open(p) for p in paths]
    cw, ch = max(i.width for i in imgs), max(i.height for i in imgs)
    rows = (len(imgs) + cols - 1) // cols
    sheet = Image.new("RGB", (cw * cols, ch * rows), (235, 235, 235))
    for k, im in enumerate(imgs):
        sheet.paste(im, ((k % cols) * cw, (k // cols) * ch))
    sheet.save(out_path)


def _parse_pages(spec: str | None, total: int) -> list[int]:
    if not spec:
        return list(range(total))
    picked: list[int] = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            picked.extend(range(int(a), int(b) + 1))
        else:
            picked.append(int(part))
    return [p for p in picked if 0 <= p < total]


def _fmt_gap(g: tuple[float, float, float], width: float) -> str:
    g0, g1, peak = g
    return f"[{g0:.0f},{g1:.0f}] 宽{g1 - g0:.0f}({(g1 - g0) / width:.3%}) 峰值occ={peak:.3f}"


def main() -> None:
    ap = argparse.ArgumentParser(description="分栏检测新算法只读探针（页号一律 0-indexed）")
    ap.add_argument("file_path")
    ap.add_argument("--channel", default="a", choices=["a", "b"], help="a=原生文字层, b=OCR版面区域")
    ap.add_argument("--pages", help="只看这些页，如 8-15 或 3,7,11（0-indexed）")
    ap.add_argument("--dump-profile", action="store_true", help="打印分箱占用率数组")
    ap.add_argument("--dump-blocks", action="store_true", help="打印每个块的bbox和文字，核对版面真相用")
    ap.add_argument("--sweep-tau", action="store_true", help="打印栏数对 τ 的敏感性")
    ap.add_argument("--render", metavar="DIR", help="渲染页面图并叠加新旧分栏线，供目视核定")
    ap.add_argument("--render-dpi", type=int, default=100)
    ap.add_argument("--montage", type=int, metavar="COLS",
                    help="把渲染出的页面图按每行COLS张拼成大图（配合 --render）")
    ap.add_argument("--only-diff", action="store_true", help="只处理新旧不一致的页")
    ap.add_argument("--flat", action="store_true",
                    help="用已证伪的扁平变体（整页一次剖面+组合枚举），仅供对比")
    ap.add_argument("--levels", action="store_true", help="打印分层结构每一层的中间量")
    ap.add_argument("--probe-tau", type=float, default=0.25,
                    help="仅用于量谷底占用率分布的宽松阈值，与工作τ解耦，避免循环论证。"
                         "取0.25：是工作τ(0.10)的2.5倍所以不循环，又落在直方图双峰之间的谷里，"
                         "不至于像0.5那样把半栏正文也吞进'谷底'")
    for key, val in DEFAULTS.items():
        ap.add_argument(f"--{key.replace('_', '-')}", type=type(val), default=val)
    args = ap.parse_args()

    cfg = {k: getattr(args, k) for k in DEFAULTS}
    cfg["flat"] = args.flat
    path = Path(args.file_path)

    print(f"文件: {path.name}")
    print(f"通道: {'A类(原生文字层)' if args.channel == 'a' else 'B类(OCR版面区域)'}")
    print(f"阈值: {cfg}")
    print("=" * 100)

    if args.channel == "a":
        pages = load_native_pages(path)
        indices = _parse_pages(args.pages, len(pages))
    else:
        import fitz
        n_phys = fitz.open(str(path)).page_count
        indices = _parse_pages(args.pages, n_phys)
        pages = load_ocr_pages(path, set(indices))

    all_gap_peaks: list[float] = []      # 谷底峰值占用率 → τ 的下界侧分布
    all_column_occ: list[float] = []     # 栏内分箱占用率 → τ 的上界侧分布
    all_bins: list[float] = []           # 全部分箱，看整体是不是双峰（完全不依赖任何阈值）
    cand_counts: list[int] = []
    summary: list[tuple[int, int, int]] = []

    computed = []
    for i in indices:
        blocks, width = pages[i]
        res = detect(blocks, width, cfg)
        old = _detect_column_boundaries_legacy(blocks, width)
        computed.append((i, blocks, width, res, old, len(old) + 1 if blocks else 1))
    if args.only_diff:
        computed = [c for c in computed if c[5] != c[3]["n_cols"]]
        print(f"（--only-diff：{len(computed)} 页新旧不一致）")

    rendered: list[Path] = []
    for i, blocks, width, res, old, old_n in computed:
        rng = res["range"]
        rng_s = f"[{rng[0]:.0f},{rng[1]:.0f}]" if rng else "-"
        cv_s = f"{res['cv']:.4f}" if res["cv"] is not None else "-"
        flag = "" if old_n == res["n_cols"] else "   <<< 新旧不一致"
        print(f"\n第{i}页(0-indexed)  页宽={width:.0f}  块数={len(blocks)}  内容范围={rng_s}")
        print(f"  旧算法栏数={old_n}  新算法栏数={res['n_cols']}  CV={cv_s}{flag}")
        print(f"  候选gap({len(res['candidates'])}):")
        for g in res["candidates"]:
            mark = " *选中" if g in res["gaps"] else ""
            print(f"    {_fmt_gap(g, width)}{mark}")
        if res["gaps"]:
            print(f"  栏宽: {[f'{w:.0f}' for w in res['widths']]}")

        cand_counts.append(len(res["candidates"]))
        # **τ标定的两组分布必须用独立的 probe_tau 量，不能用工作 τ**：候选gap是
        # "occ<=τ的连续区间"，它的峰值占用率天然被 τ 截断（用τ=0.10量出来的最大值
        # 必然≤0.10），拿它去论证"阈值取0.10有余量"是循环论证。这里改用一个宽松得多
        # 的 probe_tau 重新扫一遍谷底，让谷底真实的占用率分布完整暴露出来。
        if rng:
            bw = cfg["bin_ratio"] * width
            probe_gaps = _candidate_gaps(
                res["profile"], rng[0], bw, args.probe_tau, cfg["gap_min_width"] * width
            )
            all_gap_peaks.extend(g[2] for g in probe_gaps)
            in_gap = set()
            for g0, g1, _ in probe_gaps:
                in_gap.update(range(int((g0 - rng[0]) / bw), int((g1 - rng[0]) / bw) + 1))
            all_column_occ.extend(o for k, o in enumerate(res["profile"]) if k not in in_gap and o > 0)
            all_bins.extend(res["profile"])

        if args.levels and "levels" in res:
            names = ["主缝(整页)", "子缝(左半)", "子缝(右半)"]
            for name, lv in zip(names, res["levels"]):
                rs = f"[{lv['range'][0]:.0f},{lv['range'][1]:.0f}]" if lv["range"] else "-"
                seam_s = f"{lv['seam']:.1f}" if lv["seam"] is not None else "无"
                print(f"    {name}: 范围={rs} 缝={seam_s} 候选={len(lv['candidates'])}"
                      f" 栏宽={[f'{w:.0f}' for w in lv['widths']]}")
                for g in lv["candidates"]:
                    print(f"        {_fmt_gap(g, width)}")

        if args.dump_blocks:
            for b in sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0])):
                x0, y0, x1, y1 = b["bbox"]
                print(f"    x[{x0:.0f},{x1:.0f}] y[{y0:.0f},{y1:.0f}] "
                      f"宽{x1 - x0:.0f} | {b.get('text', '')[:60]!r}")

        if args.dump_profile:
            print("  占用率剖面:", " ".join(f"{o:.2f}" for o in res["profile"]))

        if args.sweep_tau:
            row = []
            for tau in (0.02, 0.05, 0.10, 0.15, 0.20, 0.30):
                row.append(f"τ={tau}:{detect(blocks, width, {**cfg, 'tau': tau})['n_cols']}栏")
            print("  τ敏感性:", "  ".join(row))

        if args.render:
            out_dir = Path(args.render)
            out_dir.mkdir(parents=True, exist_ok=True)
            png = out_dir / f"p{i:03d}.png"
            render_overlay(path, i, width, res, old, png, args.render_dpi)
            rendered.append(png)

        summary.append((i, old_n, res["n_cols"]))

    if args.render and args.montage and rendered:
        for k in range(0, len(rendered), args.montage * 2):
            batch = rendered[k:k + args.montage * 2]
            sheet = Path(args.render) / f"montage_{k // (args.montage * 2):02d}.png"
            make_montage(batch, sheet, args.montage)
            print(f"  拼图: {sheet}  ({[p.stem for p in batch]})")

    # ---- 汇总：标定阈值要看的就是这两组分布能不能分开 ----
    print("\n" + "=" * 100)
    print("汇总（0-indexed 页号）")
    print(f"  栏数分布  旧: {_counts([o for _, o, _ in summary])}   新: {_counts([n for _, _, n in summary])}")
    print(f"  新旧不一致的页: {[i for i, o, n in summary if o != n]}")
    print(f"  每页候选gap数: min={min(cand_counts)} max={max(cand_counts)} 分布={_counts(cand_counts)}")

    # ---- 对照 ground truth 计分：这才是验收线看的东西 ----
    gt = _gt_for(path)
    scored = [(i, o, n, gt[i]) for i, o, n in summary if i in gt]
    if scored:
        gain = [i for i, o, n, t in scored if n == t and o != t]
        regress = [i for i, o, n, t in scored if o == t and n != t]
        both_bad = [i for i, o, n, t in scored if o != t and n != t]
        print(f"\n  对照GT（{len(scored)}页有GT）: 旧对={sum(o == t for _, o, _, t in scored)}"
              f"  新对={sum(n == t for _, _, n, t in scored)}")
        print(f"    收益(旧错新对)={len(gain)} {gain}")
        print(f"    ⛔回归(旧对新错)={len(regress)} {regress}")
        print(f"    都错={len(both_bad)} {[(i, f'旧{o}新{n}→GT{t}') for i, o, n, t in scored if o != t and n != t]}")
    print(f"\n  τ标定用的两组分布（用 probe_tau={args.probe_tau} 量，与工作τ解耦）:")
    _describe("  谷底峰值占用率(应低)", all_gap_peaks)
    _describe("  栏内分箱占用率(应高)", all_column_occ)
    print("\n  全部分箱占用率直方图（不依赖任何阈值，看是否双峰）:")
    _histogram(all_bins)


def _counts(vals: list[int]) -> dict:
    return {v: vals.count(v) for v in sorted(set(vals))}


def _histogram(vals: list[float], n_buckets: int = 20) -> None:
    if not vals:
        return
    hist = [0] * n_buckets
    for v in vals:
        hist[min(n_buckets - 1, int(v * n_buckets))] += 1
    peak = max(hist) or 1
    for i, c in enumerate(hist):
        bar = "#" * int(40 * c / peak)
        print(f"    {i / n_buckets:.2f}-{(i + 1) / n_buckets:.2f} {c:6d} {bar}")


def _describe(label: str, vals: list[float]) -> None:
    if not vals:
        print(f"{label}: (无样本)")
        return
    s = sorted(vals)
    def q(p): return s[min(len(s) - 1, int(p * len(s)))]
    print(f"{label}: n={len(s)} min={s[0]:.3f} p25={q(.25):.3f} 中位={q(.5):.3f} "
          f"p75={q(.75):.3f} p95={q(.95):.3f} max={s[-1]:.3f}")


if __name__ == "__main__":
    main()
