"""A类原生PDF的字符级文本装配：同一视觉行被PyMuPDF拆成两个line时，按字符坐标还原真实顺序。

**根因**：全角开括号/前引号（`《（【“‘〈「『〔［｛`）的**墨迹只画在字框的右半边**，左半边
是空的。真实坐标（预览版活动计划第6页 `1.《数据治理框架及实践之道》`，字号8.5）：

| 字符 | 字框 x |
|---|---|
| `1` | 57.34 – 62.06 |
| `《` | **61.10** – 69.60（墨迹约从64.8才开始） |
| `.` | 62.44 – 64.80（整个落在 `《` 字框的空白左半里） |
| ` ` | 64.80 – 69.43（排版填充空格，字框几乎完全被 `《` 盖住） |

PyMuPDF 把它拆成了两个 line（`1《` / `. 数据…》`），`native_pdf.py` 的同行判定认为是同一
视觉行、用空格拼，就成了 `1《 . 数据治理框架及实践之道》`——LLM 逐条报"书名号里多了个句点"，
一份刊物里几十条假错误全出自这里。

**已证伪的两个想法**（都在全量样本上实测过）：按字框**左边界**排序修不好（`《`.x0 = 61.10
本来就小于 `.`.x0 = 62.44，排出来一模一样）；**无条件**按字框中心排序会把本来正确的行排坏
（没被拆行的 `1.《…` 原顺序就是对的，中心排反而会把 `.` 挪到 `《` 后面）。

**成立的做法**：只对开括号那一类取字框**中心**当排序坐标，其余字符取**左边界**。闭括号/
后引号的墨迹在字框左半，左边界本来就对得上视觉位置，**明确不做修正**（`_INK_RIGHT_HALF_CHARS`
里不收，测试里钉了一条防"顺手对称处理"）。

## 什么时候才动字符级合并：让排序结果自己说话

判据不是"两行 x 范围有没有重叠"——真实样本里 `课程介绍`(574.7–603.6) 与 `讲师介绍`
(603.4–624.3) 这种**表头并排**的情况也有 0.2pt 重叠，但它们只是挨着，并没有交错，按字符
拼会把中间那个必要的分隔空格吃掉，变成 `课程介绍讲师介绍`。

改成**先排序、再看排序结果有没有真的交错**：

- 排完仍是"A 的字符全在前、B 的全在后" → 说明只是挨着，**沿用原来的空格拼接**（行为不变）。
- 排完变成"B 全在前、A 全在后" → 整体前后颠倒（PyMuPDF 给的行序与视觉顺序相反，如
  `合肥`(x393) 排在 `9月17-18日`(x332) 前面），按视觉顺序**空格拼接**。
- 排完两行字符真的交错 → 这才是开括号错位那一类，**按字符拼**并丢掉排版填充空格。

这样"该合并的合并、只是挨着的不动"，不需要再去猜一个重叠比例阈值。
"""

from __future__ import annotations

import re
import statistics

import config

# 墨迹画在字框右半边的全角标点：只收开括号与前引号。闭括号/后引号墨迹在左半、字框左边界
# 与视觉位置本来就一致，收进来反而会把正确的顺序排坏。改这个集合要重跑全文本 diff 验收。
_INK_RIGHT_HALF_CHARS = "《（【“‘〈「『〔［｛"

_LATIN_RE = re.compile(r"[A-Za-z0-9]")


def _chars_text(chars: list[dict]) -> str:
    """字符列表还原成文本。"""
    return "".join(c["c"] for c in chars)


def _strip_edge_spaces(chars: list[dict]) -> list[dict]:
    """去掉一行首尾的空白字符——相当于对字符列表做 `str.strip()`。

    行首尾的空白在拼接时一律由拼接符（空格/换行）取代，留着只会多出重复空格；而中间
    的空白要原样保留（英文词间隔），所以不能简单地全删。
    """
    lo, hi = 0, len(chars)
    while lo < hi and chars[lo]["c"].isspace():
        lo += 1
    while hi > lo and chars[hi - 1]["c"].isspace():
        hi -= 1
    return chars[lo:hi]


def restore_unmapped_glyph_spaces(span_chars: list[list[dict]]) -> None:
    """把"其实是词间空格"的未映射占位码位改写成真空格（就地改 `c` 字段）。

    `native_pdf.py::_strip_unmapped_glyph_chars` 把 C0 占位码位整个删掉，因为它们绝大多数
    是设计软件画的项目符号/小箭头。但**同一个码位也被用来编码词间空格**：`AI Agent` 在
    PDF 里是 `A I \\x01 A g e n t`，那个 `\\x01` 占 0.224 个字号宽的实际前进宽度、视觉上就是
    个窄空格。整个删掉就成了 `AIAgent`，LLM 逐条报"缺空格"——这不是原文的内容问题，是我们
    自己删出来的。

    **判据是两侧都是拉丁字母/数字**，和 `_drop_tracking_spaces` 的限定完全同源。全量实测
    12 份样本里这样的占位码位有 150 个、141 种上下文，**逐条看过全部是词边界**
    （`Plant␁Design`、`GE␁Vernova`、`AVEVA␁E3D␁Design`、`OPC␁UA␁Fx`、`Point␁Cloud␁Manager`），
    没有一个是装饰符号。宽度分不开（装饰的和当空格用的都恰好是 0.224 个字号），只有位置分得开。

    代价是汉字与拉丁之间那种（`专家论道␁␁Expert`）仍按装饰删掉，中文排版里那里本来就不留
    空格，属于宁可漏修的一侧。

    传入的是**按 span 分组的**字符列表：判定要看行内相邻字符，而相邻的两个字符可能分属不同
    span（`2026␁e-works` 就是），所以必须先跨 span 拉平了看，再交回各 span 走后续的按 span
    字距判定。
    """
    flat = [ch for chars in span_chars for ch in chars]
    for i, ch in enumerate(flat):
        if ord(ch["c"]) >= 0x20 or i == 0 or i + 1 >= len(flat):
            continue
        if _LATIN_RE.match(flat[i - 1]["c"]) and _LATIN_RE.match(flat[i + 1]["c"]):
            ch["c"] = " "


def _ink_adjusted_x(char: dict) -> float:
    """这个字符**看上去**从哪里开始——用作同一视觉行内字符排序的坐标。

    绝大多数字符取字框左边界；墨迹偏在字框右半的全角开括号取字框中心，否则它的左边界会
    比实际视觉位置偏左约一个半字宽，把紧挨在它前面的窄字符（如 `.`）挤到它后面去。
    """
    x0, x1 = char["bbox"][0], char["bbox"][2]
    return (x0 + x1) / 2 if char["c"] in _INK_RIGHT_HALF_CHARS else x0


def sort_line_by_ink(chars: list[dict]) -> list[dict]:
    """把一行内的字符按"看上去在哪"重排（稳定排序），修 `2《. 航空…》` 这类字序错乱。

    PDF 流里的字符顺序是 `2 《 .`，视觉上却是 `2.《`——`.` 的字框整个落在 `《` 那半边空白里
    （坐标见本模块顶部表格）。`_merge_same_row_chars` 早就会按同样的坐标重排，但它只在
    **PyMuPDF 把一行拆成了两个 line** 时才跑；这两处没被拆行，就一直漏着。判据只看坐标、
    与行有没有被拆开无关，本来就该每行都跑。

    排序坐标沿用 `_ink_adjusted_x`（只有开括号取字框中心，其余取左边界）。全量实测 12 份
    样本只有 **17 行**顺序发生变化：2 行是上述修复，另 15 行变的只是 `《` 与占位码位/填充
    空格的**中间**先后，而那些字符随后都会被删掉——**最终文本只有那 2 个块变了**，其余逐
    字节不变。

    顶部 docstring 记着"无条件按字框中心排序会把正确的行排坏"，**那条在当前语料上复现不
    出来**（全中心排与本函数的差异同样只落在随后会被删掉的空格上）。它成立与否不影响这里
    的选择——`_ink_adjusted_x` 已经是既有设计，本函数只是把它的适用范围从"行被拆开时"扩到
    "每一行"——但别把它当成本函数背后的实测依据。
    """
    return [ch for _, ch in sorted(enumerate(chars), key=lambda t: _ink_adjusted_x(t[1]))]


def drop_filler_spaces(chars: list[dict]) -> list[dict]:
    """丢掉字框被相邻字符盖住的空格——那是排版填充，不是词间隔。

    `1.《数据…》` 里 `.` 与 `《` 之间那个空格字框 64.80–69.43 几乎整个落在 `《` 的
    61.10–69.60 里面，是排版软件为对齐塞的填充；真正的词间隔空格与左右邻字都不重叠。
    按"与任一侧邻字的横向重叠超过自身宽度 config.NATIVE_FILLER_SPACE_MIN_COVER_RATIO"判定。

    **每一行都要过一遍，不是只在字符级合并时过**：`1.《 AIAgent技术全景与工业应用解析》`
    这一处 PyMuPDF 没把行拆开，走不到 `_merge_same_row_chars`，那个躲在 `《` 字框里的填充
    空格就原样留着，LLM 报"书名号后多了空格"。判据只看坐标、与行有没有被拆开无关，本来就
    该无条件跑。
    """
    kept: list[dict] = []
    for i, ch in enumerate(chars):
        if not ch["c"].isspace():
            kept.append(ch)
            continue
        x0, x1 = ch["bbox"][0], ch["bbox"][2]
        width = x1 - x0
        if width <= 0:
            continue  # 零宽空格本就不占位，留着只会在文本里凭空多一个空格
        covered = 0.0
        for nb in (chars[i - 1] if i else None, chars[i + 1] if i + 1 < len(chars) else None):
            if nb is None:
                continue
            covered = max(covered, min(x1, nb["bbox"][2]) - max(x0, nb["bbox"][0]))
        if covered / width < config.NATIVE_FILLER_SPACE_MIN_COVER_RATIO:
            kept.append(ch)
    return kept


def _drop_tracking_spaces(chars: list[dict]) -> list[dict]:
    """删掉"排版字距微调被编码成真空格"的假空格：`APS` 被拆成 `A PS`、`BOM` 被拆成 `B O M`。

    这些是真的 U+0020，但不是词间隔——排版软件给一段文字加了字间距（tracking），导出 PDF
    时把字间距写成了空格字符。三份期刊里 100 多处，`Autodesk`→`A u t o d e s k`、
    `2025年`→`2 0 2 5年`、`48.6%`→`4 8 . 6 %` 都是。

    ## 判据：空格宽度 vs **同一 span 内字母之间的间隙**，不是"空格宽度 / 字号或众数空格宽"

    **"按该(字体,字号)下最常见的空格宽度归一化"这条已证伪**，不要再试：这几份期刊是两端
    对齐排版，真词间空格会被压缩，`Plant Design`(0.45)、`Shadow Robot`(0.47)、
    `CEO Peter`(0.52)、`AI Agent`(0.62)、`3D CAD`(0.69) 这些**真空格**的比值和假空格的
    0.46~0.58 完全交叠，79期尤其没有任何阈值分得开。（"按空格宽/字号"和"按该字体最宽
    空格归一化"两条更早就证伪了，前者 `Mirko Kovač` 0.266 vs `I I o T` 0.256 几乎一样。）

    成立的判据是局部的：**字距微调是整段均匀施加的，所以假空格的宽度恰好等于同一段里
    字母之间的间隙**；真词间空格则远宽于字母间隙。全量实测分得极干净：

    | | r = 空格宽 / 同span非空格字符间隙的中位数 |
    |---|---|
    | 假空格（字距微调） | 0.94 ~ 1.40 |
    | 真词间空格 | ≥ 4.0（普通紧排文本里字母间隙≈0，比值上百） |

    中间 2~3 这一段**一个样本都没有**，阈值取 2.0，两侧余量 0.6 / 2.0。

    按 span 而不是按行判定：字距是 span 级的排版属性，跨 span 混算会把两种排版的间隙搅在
    一起。样本量不足（非空格字符对少于 `config.NATIVE_TRACKING_SPACE_MIN_GAP_SAMPLES`）时
    直接不处理——中位数不可靠，宁可漏修也不误删。

    **限定两侧都是拉丁字母/数字**：一是这类假空格实测全出现在英文/数字串里，二是汉字与
    URL 之间那类真空格（`数字化企业网  www.e-works`，几百处）不该进删除范围。代价是
    `2 0 2 6 年` 里 `6` 与 `年` 之间那个假空格修不掉，属于宁可漏修的一侧。
    """
    gaps = [
        chars[i + 1]["bbox"][0] - chars[i]["bbox"][2]
        for i in range(len(chars) - 1)
        if chars[i]["c"] != " " and chars[i + 1]["c"] != " "
    ]
    if len(gaps) < config.NATIVE_TRACKING_SPACE_MIN_GAP_SAMPLES:
        return chars
    median_gap = statistics.median(gaps)
    if median_gap <= 0:
        return chars  # 字母紧贴排布，没有字距微调可言
    limit = median_gap * config.NATIVE_TRACKING_SPACE_MAX_GAP_RATIO
    kept: list[dict] = []
    for i, ch in enumerate(chars):
        if (
            ch["c"] == " "
            and 0 < i < len(chars) - 1
            and _LATIN_RE.match(chars[i - 1]["c"])
            and _LATIN_RE.match(chars[i + 1]["c"])
            and ch["bbox"][2] - ch["bbox"][0] < limit
        ):
            continue
        kept.append(ch)
    return kept


def _overlap_within_one_char(chars_a: list[dict], chars_b: list[dict]) -> bool:
    """两段字符的横向重叠区，是不是窄到只有一个字宽以内。

    本模块要修的错位，成因是"某个字符的字框落进了相邻字符的空白半边"，所以**串错的位置
    最多只有一个字那么宽**（`1.《数据…》` 那处重叠 7.16pt，一个全角字宽 8.5pt）。

    真实样本里有一种反例必须挡住：79期第19页股票表格的 `*ST` 与 `⼯智` 被 PyMuPDF 拆成
    两个 line，但两者 x 范围**几乎完全重合**（708.15–721.06 vs 707.36–722.26，重叠
    13.7pt ≈ 两个字宽）——它们是叠印在同一位置的两段文字，不是首尾相接，逐字符排会排成
    `⼯*S智T`。用"重叠不超过一个字宽"把这类挡在字符级合并之外，退回原来的空格拼接。

    尺子取两段里最宽的那个字符，而不是写死一个 pt 值：字号随版面变，用文档自己的字宽
    当尺子才对不同刊物自适应。
    """
    x1 = min(max(c["bbox"][2] for c in chars_a), max(c["bbox"][2] for c in chars_b))
    x0 = max(min(c["bbox"][0] for c in chars_a), min(c["bbox"][0] for c in chars_b))
    overlap = x1 - x0
    widest = max(c["bbox"][2] - c["bbox"][0] for c in chars_a + chars_b)
    return overlap <= widest


def _merge_same_row_chars(chars_a: list[dict], chars_b: list[dict]) -> list[dict] | None:
    """把已判定为同一视觉行的两段字符按视觉顺序合并，返回合并后的字符列表。

    返回 None 表示"排序后两段并没有真的交错、只是一前一后挨着"——调用方应沿用原来的
    空格拼接。两段整体前后颠倒也算没交错，但返回的是颠倒后的顺序（调用方照样用空格拼），
    判据与理由见本模块顶部 docstring。
    """
    if not _overlap_within_one_char(chars_a, chars_b):
        return None
    n_a = len(chars_a)
    tagged = [(i, ch) for i, ch in enumerate(chars_a + chars_b)]
    # 稳定排序：同一坐标上的字符保持各自段内的原有先后
    tagged.sort(key=lambda t: _ink_adjusted_x(t[1]))
    order = [i for i, _ in tagged]
    if order == sorted(range(n_a)) + sorted(range(n_a, n_a + len(chars_b))):
        return None                       # A 全在前、B 全在后：只是挨着
    if order == sorted(range(n_a, n_a + len(chars_b))) + sorted(range(n_a)):
        return None                       # B 全在前、A 全在后：整体颠倒，仍不算交错
    return drop_filler_spaces([ch for _, ch in tagged])


def _reorder_same_row_lines(chars_a: list[dict], chars_b: list[dict]) -> bool:
    """两段同一视觉行的字符，视觉上是不是 b 整个在 a 左边（PyMuPDF 给的行序与视觉顺序相反）。

    只在 `_merge_same_row_chars` 判定"没有交错"、要退回空格拼接时用来定先后。真实样本里
    `合肥`(x393) 被 PyMuPDF 排在 `9⽉17-18⽇`(x332) 前面，`e-works` 与 `动态`、
    `国内考察` 与 `考察简介` 同理，按视觉顺序调过来才读得通。

    **要求两段 x 范围完全不重叠才调**：重叠说明两段挤在同一片位置（如 79期第19页股票
    表格里叠印的 `*ST` 与 `⼯智`，只差 0.79pt 就会被判成"b 在左边"），这种情况下谁前谁后
    没有可靠的坐标依据，保持 PyMuPDF 给的原序更稳妥。
    """
    if not chars_a or not chars_b:
        return False
    return max(c["bbox"][2] for c in chars_b) <= min(c["bbox"][0] for c in chars_a)
