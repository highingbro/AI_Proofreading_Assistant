"""A类原生PDF的块级段落还原：把"被栏宽顶回来的续写块"并回上一块。

**根因**：这类期刊里 PyMuPDF 的一个 block 常常就是一个视觉行，同一段话被栏宽切开后落在
**相邻的两个 block** 里：

```
第5页第1栏"• 讲解AI Agent关键技术与工业典型应用场"     ← 一个 block
第5页第1栏"景，助力企业把握技术前沿。"                   ← 另一个 block
```

`core/chunker/` 在 block 之间要插 `\\n`（原因见 core/chunker/CLAUDE.md），LLM 于是看到
"…工业典型应用场\\n景，助力企业…"这种词中断行，把它当成多余空格/漏字报出来。真实数据库
统计：一次校对 108 条问题里 49 条是这么来的，纯噪声。

**为什么接在块级、而且必须在分栏排序之后**：先按直觉把它接在 `_extract_native_page_raw`
的行拼接里试过三种接法，全部失败——那 49 条噪声的换行**根本不在 block 内部**，块内怎么拼
都碰不到它；而判"这一行有没有顶到最右"需要知道**本栏**的文字右边距，那是
`_order_native_page` 跑完、每个块有了 `column` 字段之后才知道的。这也是
`_is_wrapped_continuation` 当初接不上的原因：提取阶段没有分栏信息。

**七道闸门每一道都对应一类实测到的误合并**，不要因为"看着冗余"删掉任何一道：

| 闸门 | 挡住的真实误合并 |
|---|---|
| 两块同栏且都不是 `span` | 跨栏标题被并进正文 |
| 平均字号相差 ≤ `NATIVE_WRAP_SAME_SIZE_TOLERANCE` | 大标题被并进正文 |
| 上一块**最后一行**顶到本栏文字右边距 | 两行标题、目录页码 |
| 上一块最后一行宽度 ≥ `NATIVE_WRAP_MIN_FULL_LINE_CHARS` 个字宽 | `2 ⟪+⟫ 论坛计划`、竖排刊名 `二 ⟪+⟫ O⏎二⏎六` |
| 下一块不以列表标记开头 | `• 华润三九… ⟪+⟫ • 格力电器…` |
| 上一块不含点线引导 | 目录行 `………3 ⟪+⟫ （二）进入…` |
| 纵向紧邻 | 跨段落/跨版块 |

**用"最后一行"的 x1 和字号、不是块 bbox 的**：块 bbox 的 x1 会被块内更长的行顶替，实测
因此把 `5. 目前的局限性` 这种小标题误判成续写行（它上面那行更长、顶到了栏右）。

**这一步会减少 block 数量**——合并的是"本来就属于同一段的两块"，`block_index` 的连续性与
单调性不变，但 chunk 边界会变，见 core/parser/CLAUDE.md 对应一节。
"""

from __future__ import annotations

import re

import config

# 以这些字符收尾的行，视为"内容说完了主动换行"，不当续写行处理。只收句末/收束类
# 标点，不含"，、"——逗号顿号收尾恰恰是句子没说完、下一行还要接着排的强信号。
_LINE_STOP_CHARS = "。！？；…：.!?;:）)》」』】〕》\"'”’"

# 下一块以列表标记开头 → 它是新的一项，不是上一项的续写。项目符号、阿拉伯数字序号
# （`1.`/`(1)`/`（1）`）、中文数字序号、带圈数字四类，覆盖实测到的全部误合并样本。
_LIST_MARKER_RE = re.compile(r"^\s*([•·▪◦‣⁃]|[（(]?\d{1,2}[）).、]|[一二三四五六七八九十]+[、.]|[①-⑳])")

# 上一块含连续3个以上的点线引导 → 它是目录行（`标题………3`），下一块是目录的下一条。
_DOT_LEADER_RE = re.compile(r"[.。·・…]{3,}")

_LATIN_RE = re.compile(r"[A-Za-z0-9]")


def _is_wrapped_continuation(text: str, bbox: tuple[float, float, float, float], right_edge: float, font_size: float) -> bool:
    """这一行是不是"被栏宽顶到头才换行"的续写行——即下一行是同一句话的直接延续。

    两个信号必须同时成立才算（都不成立的判法太松，会把标题/列表项跟正文粘成病句）：
    收尾没有句末/收束标点（`_LINE_STOP_CHARS`），且右边界离本栏最右不足一个字的宽度
    （剩不下一个字，说明这行是被宽度顶回来的，不是内容说完了主动换的）。一个字的宽度
    用字号近似——中文全角字符宽度就约等于字号。
    """
    if not text or text[-1] in _LINE_STOP_CHARS:
        return False
    tolerance = max(font_size, 1.0) * config.NATIVE_WRAP_RIGHT_EDGE_TOLERANCE_CHARS
    return bbox[2] >= right_edge - tolerance


def _lines_vertically_adjacent(bbox_a: tuple[float, float, float, float], bbox_b: tuple[float, float, float, float]) -> bool:
    """b 是不是紧接在 a **下面**的一行（而不是同一行的另一格、或隔了一个段落间距/版块）。

    两侧都要挡，少一侧都会出事：

    - **上界**：间距不超过 a 自身高度的 `NATIVE_WRAP_MAX_LINE_GAP_RATIO` 倍，挡跨段/跨版块。
    - **下界**：b 不能跟 a 纵向重叠到 `NATIVE_SAME_ROW_OVERLAP_MIN_RATIO`——那说明它们本来就
      在同一视觉行上。表格行 `制造业AIAgent实战落地特训营` | `合肥` 实测 y 只差 0.63pt、重叠
      92%，横向却隔了 22 倍行高，`_lines_share_same_row` 的横向闸门会放行它；只判"间距 ≤ 上界"
      的话，负间距天然满足，两个单元格就被当成上下两行拼成了病句。真正的下一行重叠为 0。
    """
    height_a = bbox_a[3] - bbox_a[1]
    if height_a <= 0 or bbox_b[1] <= bbox_a[1]:
        return False
    overlap = min(bbox_a[3], bbox_b[3]) - max(bbox_a[1], bbox_b[1])
    smaller_height = min(height_a, bbox_b[3] - bbox_b[1])
    if smaller_height > 0 and overlap / smaller_height >= config.NATIVE_SAME_ROW_OVERLAP_MIN_RATIO:
        return False
    return (bbox_b[1] - bbox_a[3]) <= height_a * config.NATIVE_WRAP_MAX_LINE_GAP_RATIO


def _local_right_edge(bboxes: list[tuple], bbox: tuple) -> float:
    """本行所在这一竖排的文字右边距 = 块内所有与它横向有重叠的行的 x1 最大值。

    **不能拿整个块的最右**：PyMuPDF 会把并排的两个单元格塞进一个 block（实测活动日历页有个
    块含左右两格共 4 行），拿另一格的右边界当尺子，本格的满行永远够不着、一处也合不上。
    """
    edge = bbox[2]
    for other in bboxes:
        if min(bbox[2], other[2]) - max(bbox[0], other[0]) > 0:
            edge = max(edge, other[2])
    return edge


def line_join_separator(
    prev_text: str, prev_bbox: tuple, prev_size: float,
    cur_text: str, cur_bbox: tuple, cur_size: float, right_edge: float,
) -> str | None:
    """块内相邻两行该用什么拼：`""`/`" "` 表示是同一句被宽度顶断的，`None` 表示该换行。

    判据和闸门与块级合并（`_should_join`）完全一致，只是尺子换成"本竖排的右边距"。同一件事
    在块内块外各出现一次，是因为 PyMuPDF 的 block 划分对这类排版毫无规律：正文长段被切开时
    两截通常落在**两个 block**，而表格/日历单元格里的两行标题（`AI赋能设备全寿命周期` /
    `管理高级研修班`）落在**一个 block 的两个 line**。少做哪一边都会剩下一大片词中断行。
    """
    if abs(prev_size - cur_size) > config.NATIVE_WRAP_SAME_SIZE_TOLERANCE:
        return None
    if (prev_bbox[2] - prev_bbox[0]) < max(prev_size, 1.0) * config.NATIVE_WRAP_MIN_FULL_LINE_CHARS:
        return None
    if _LIST_MARKER_RE.match(cur_text) or _DOT_LEADER_RE.search(prev_text):
        return None
    if not _is_wrapped_continuation(prev_text, prev_bbox, right_edge, prev_size):
        return None
    if not _lines_vertically_adjacent(prev_bbox, cur_bbox):
        return None
    return _separator(prev_text, cur_text)


def _separator(prev_text: str, cur_text: str) -> str:
    """断裂处两侧都是拉丁字母/数字时补一个空格，否则空拼——理由见 `_join`。"""
    if prev_text and cur_text and _LATIN_RE.match(prev_text[-1]) and _LATIN_RE.match(cur_text[0]):
        return " "
    return ""


def _column_right_edges(blocks: list[dict]) -> dict:
    """每一栏的文字右边距 = 该栏所有非跨栏块 x1 的最大值。

    用块 bbox 而不是"最后一行"的 x1：这里要的是**整栏**能排到多右，栏里任何一个满行都
    是这个上界的证据；判某一行有没有顶到这个上界才必须用那一行自己的坐标。
    """
    edges: dict = {}
    for b in blocks:
        col = b.get("column")
        if col == "span":
            continue
        edges[col] = max(edges.get(col, 0.0), b["bbox"][2])
    return edges


def _should_join(prev: dict, cur: dict, right_edge: float | None) -> bool:
    """prev 的末尾和 cur 的开头是不是同一段被栏宽切开的两截。闸门顺序按代价从小到大排。"""
    if right_edge is None:
        return False
    if prev.get("column") != cur.get("column") or prev.get("column") == "span":
        return False
    if abs(prev["avg_size"] - cur["avg_size"]) > config.NATIVE_WRAP_SAME_SIZE_TOLERANCE:
        return False
    last_bbox, last_size = prev["last_row_bbox"], prev["last_row_size"]
    if (last_bbox[2] - last_bbox[0]) < max(last_size, 1.0) * config.NATIVE_WRAP_MIN_FULL_LINE_CHARS:
        return False
    if _LIST_MARKER_RE.match(cur["text"]):
        return False
    if _DOT_LEADER_RE.search(prev["text"]):
        return False
    if not _is_wrapped_continuation(prev["text"], last_bbox, right_edge, last_size):
        return False
    return _lines_vertically_adjacent(last_bbox, cur["bbox"])


def _join(prev: dict, cur: dict) -> dict:
    """把 cur 并进 prev，返回合并后的新块（不改原块）。

    **默认用空字符串拼**：中文续写行之间原文就没有任何分隔符，插空格会被LLM读成多余空格。
    **只有断裂处两侧都是拉丁字母/数字时补一个空格**——排版软件在词边界处折行，那个词间
    空格不会被写进 PDF，空拼会造出 `GEVernova`、`Plant DesignSoftware` 这种原文没有的
    连写词，LLM 照样报"缺空格"。全量实测 4586 处合并里只有 30 处两侧都是拉丁/数字，逐条
    看过 28 处确是词边界；代价是真正被拦腰断开的西文单词会多一个空格，实测这类只有 2 处
    （`Building|and`、`Field|eXchange`，且两处其实也是词边界），两侧都远小于收益。

    `avg_size` 按字符数加权，免得一个两字的短尾巴把整段的字号基准带偏；"最后一行"的
    坐标/字号取自 cur，合并块继续参与下一轮判定时问的就是它的末行。
    """
    n_prev, n_cur = len(prev["text"]), len(cur["text"])
    total = n_prev + n_cur
    merged = dict(prev)
    merged["text"] = prev["text"] + _separator(prev["text"], cur["text"]) + cur["text"]
    merged["bbox"] = (
        min(prev["bbox"][0], cur["bbox"][0]),
        min(prev["bbox"][1], cur["bbox"][1]),
        max(prev["bbox"][2], cur["bbox"][2]),
        max(prev["bbox"][3], cur["bbox"][3]),
    )
    merged["avg_size"] = (prev["avg_size"] * n_prev + cur["avg_size"] * n_cur) / total if total else prev["avg_size"]
    merged["last_row_bbox"] = cur["last_row_bbox"]
    merged["last_row_size"] = cur["last_row_size"]
    return merged


def merge_wrapped_blocks(ordered: list[dict]) -> list[dict]:
    """把阅读顺序里相邻、且判定为同一段两截的块合并。传入的是 `_order_native_page` 的输出。"""
    if not ordered:
        return ordered
    right_edges = _column_right_edges(ordered)
    merged: list[dict] = [dict(ordered[0])]
    for cur in ordered[1:]:
        prev = merged[-1]
        if _should_join(prev, cur, right_edges.get(prev.get("column"))):
            merged[-1] = _join(prev, cur)
        else:
            merged.append(dict(cur))
    return merged
