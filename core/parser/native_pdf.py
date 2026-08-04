"""A类：有文字层 PDF 解析（从 core/parser.py 拆分而来，逻辑未改动）。

用 PyMuPDF 按坐标提取文本块，手写页眉页脚剔除 + 分栏检测 + 阅读顺序还原。
"""

from __future__ import annotations

import bisect
import re
import statistics
from collections import Counter

import config
from core.parser._cjk_variants import normalize_cjk_variants
from core.parser._columns import _detect_column_boundaries
from core.parser._common import _source_location
from core.parser._glyphs import (
    _chars_text,
    _drop_tracking_spaces,
    _merge_same_row_chars,
    _reorder_same_row_lines,
    _strip_edge_spaces,
    drop_filler_spaces,
    restore_unmapped_glyph_spaces,
    sort_line_by_ink,
)
from core.parser._paragraphs import _local_right_edge, line_join_separator, merge_wrapped_blocks

_NUMERIC_ZONE_RE = re.compile(r"^[\dIVXLCDMivxlcdm\-\.\s]{1,10}$")      # 匹配纯页码/罗马数字页码/带破折号的页码范围/带逗号，长度不超过10字符


def _zone_of(bbox: tuple[float, float, float, float], height: float) -> str:
    """判断一个文本块的坐标框(bbox)落在页面的顶部/底部/正文区。"""
    y0, y1 = bbox[1], bbox[3]  # bbox = (x0, y0, x1, y1)，y0是上边界，y1是下边界
    if y1 <= height * config.HEADER_FOOTER_ZONE_RATIO:
        return "top"     # 整个块都在页面顶部8%范围内 → 可能是页眉
    if y0 >= height * (1 - config.HEADER_FOOTER_ZONE_RATIO):
        return "bottom"  # 整个块都在页面底部8%范围内 → 可能是页脚
    return "body"         # 其余都算正文区域


def _lines_share_same_row(bbox_a: tuple[float, float, float, float], bbox_b: tuple[float, float, float, float]) -> bool:
    """判断PyMuPDF给出的两个"line"是否其实是同一视觉行被误拆成的两段。

    起因：真实文档里项目符号常用符号字体（如Wingdings）+ 和正文之间留一段缩进间隙，
    PyMuPDF的行聚类算法偶尔会因为字体切换/水平间隙过大，把明明在同一水平位置的"符号+正文"
    拆成两个独立的line对象——y轴（bbox第2/4个数字，即上下边界）却几乎完全重叠，真正在纵向
    上不同行的line之间y轴不会有这种重叠。用y轴重叠长度占较小line自身高度的比例判断，
    真实数据验证过：误拆的同一行重叠比例是100%，真正不同行是0，阈值
    config.NATIVE_SAME_ROW_OVERLAP_MIN_RATIO 取值留了充分余量。

    纵向重叠是必要条件但不充分：跨页对开版面（一个物理页印着两个页码的左右两页）里，
    左页和右页同一水平线上的两块**毫不相干**的文字，纵向同样可以100%重叠，PyMuPDF照样
    把它们聚成一个block。所以再加一道横向闸门——两个line的水平间隙不能超过较小那个line
    自身高度的 config.NATIVE_SAME_ROW_MAX_GAP_HEIGHT_RATIO 倍（行高≈字号≈一个汉字宽度，
    这个倍数约等于"最多隔几个字"）。用行高当尺子而不是绝对pt值，是为了对不同字号/不同
    渲染尺度的文档自适应。间隙为负（两个line横向有交叠）时自然通过，不需要另外判断。
    """
    y0_a, y1_a = bbox_a[1], bbox_a[3]
    y0_b, y1_b = bbox_b[1], bbox_b[3]
    overlap = min(y1_a, y1_b) - max(y0_a, y0_b)
    if overlap <= 0:
        return False
    smaller_height = min(y1_a - y0_a, y1_b - y0_b)
    if smaller_height <= 0:
        return False
    if overlap / smaller_height < config.NATIVE_SAME_ROW_OVERLAP_MIN_RATIO:
        return False
    gap = max(bbox_a[0], bbox_b[0]) - min(bbox_a[2], bbox_b[2])
    return gap <= smaller_height * config.NATIVE_SAME_ROW_MAX_GAP_HEIGHT_RATIO


def _strip_unmapped_glyph_chars(text: str) -> str:
    """删掉 C0 控制字符（`\\n` 除外）——它们是"字体没给这个字形提供Unicode映射"的占位码位。

    根因和 `_cjk_variants.py` 处理的那类是同一个（PDF字体的ToUnicode CMap有缺陷），但表现
    和处置相反：康熙部首那类映到了"看得懂、只是码位不对"的汉字，要**归一化成正确的字**；
    这类映不出任何字符、PyMuPDF只能给个 `U+0001` 占位，而且它们本来就**不是文字**——实测
    出现位置全是 `\\x01活动日历`、`\\x01\\x01抢占席位`、`e-works\\x01` 这种，是设计软件画的
    项目符号/小箭头/CTA图标，所以要**整个删掉**而不是替换。三份期刊共1637个，普通
    Word→PDF（`sample.pdf`）里0个，是"设计软件导出的PDF"特有的。

    **保留 `\\n`**：那是本模块自己拼接block内多行时插入的分隔符，不是PDF里的字符。制表符
    等其他C0字符实测不出现；即使出现也照删——PDF里的字间距完全由坐标决定，不靠制表符
    排版，删掉不丢信息。

    **私有使用区（PUA）的字符不在处理范围内，不要顺手加进来**：那是另一回事——Wingdings
    之类符号字体的项目符号（如 `U+F0D8`，武昌首义手册里88个），它承载"这是一个列表项"的
    语义，已经由 `_lines_share_same_row` 用空格拼进正文（见 core/parser/CLAUDE.md
    "PyMuPDF把同一视觉行误拆成两个line时"一节），删掉反而会丢信息。
    """
    return "".join(ch for ch in text if ch == "\n" or not _is_unmapped_glyph_char(ch))


def _is_unmapped_glyph_char(ch: str) -> bool:
    """单个字符是不是上述占位码位。字符级装配（`_glyphs.py`）按字符过滤，用得上这一支。"""
    return ord(ch) < 0x20 or ord(ch) == 0x7F


def _extract_native_page_raw(page: "fitz.Page") -> tuple[list[dict], float]:
    """从有文字层的PDF页面里，按PyMuPDF给出的坐标提取所有文本块的原始信息。

    这里只是"提取"，不做分栏、不做阅读顺序还原、不剔除页眉页脚——
    那些都是后续步骤（_strip_headers_footers / _finalize_native_page）的事。
    """
    # 用 rawdict 而不是 dict：两者的 blocks→lines→spans 结构和字段完全一致（真实文档
    # 逐字段比对过，无差异），只是每个 span 多一个 `chars` 列表给出**单个字符**的 bbox。
    # 字符级 bbox 是还原同一视觉行内真实字序所必需的——全角开括号的墨迹只占字框右半，
    # span 级坐标看不出这件事（见 core/parser/_glyphs.py）。
    data = page.get_text("rawdict")
    width, height = data["width"], data["height"]
    out: list[dict] = []
    for b in data["blocks"]:
        if b.get("type") != 0:
            continue  # type!=0 是图片块，跳过（只保留文字块）
        lines_chars: list[list[dict]] = []
        line_bboxes = []
        line_sizes = []
        sizes = []
        for line in b["lines"]:
            # 一个block可能包含多行(line)，每行又由多个span组成（比如中途换了字体/字号）
            # 在**拼接之前**逐行删占位码位：整行只有装饰图标时它会变成空串、连同它的bbox
            # 一起不进 line_bboxes，相邻两行的同行判定就直接对彼此做，不会被一个不含文字的
            # "行"隔开。放到拼完之后再删就晚了。
            # 假空格（排版字距微调）逐span判定后删除——字距是span级的排版属性，跨span
            # 混算会把两种排版的字符间隙搅在一起，见 _glyphs.py::_drop_tracking_spaces
            # 先把"其实是词间空格"的占位码位改写成真空格（跨span看邻字，见
            # _glyphs.py::restore_unmapped_glyph_spaces），剩下的才是真装饰图标、照删。
            # 顺序不能倒：删完就看不出它两侧是不是拉丁字母了。
            span_chars = [list(s["chars"]) for s in line["spans"]]
            restore_unmapped_glyph_spaces(span_chars)
            chars = []
            for sc in span_chars:
                chars.extend(
                    _drop_tracking_spaces([c for c in sc if not _is_unmapped_glyph_char(c["c"])])
                )
            # 按墨迹位置重排字序、清掉被相邻字符盖住的填充空格：这两件事判据都只看坐标，
            # 与"PyMuPDF 有没有把这一行拆成两个 line"无关，所以每行都要跑，不能像原来那样
            # 只挂在字符级合并那条路径上（`1.《 AIAgent…》`、`2《. 航空…》` 都是漏在这里的）。
            chars = _strip_edge_spaces(drop_filler_spaces(sort_line_by_ink(chars)))
            if chars:
                lines_chars.append(chars)
                line_bboxes.append(line["bbox"])
                line_sizes.append(max((s["size"] for s in line["spans"]), default=0.0))
            sizes.extend(s["size"] for s in line["spans"])  # 记录这一行里每个span的字号
        # 拼接block内多行文本：默认用换行符——block内每一行在原文里通常是独立的一行
        # （标题+正文、列表项、版式换行等不同语义单元），拼成空格会让LLM把它们读成一句
        # 连续的话，误判成"语法/标点问题"。但相邻两行若判定为_lines_share_same_row
        # （PyMuPDF把同一视觉行错误拆成了两个line），就不该换行，此时再分两种拼法：
        # 两段字符按坐标排完真的交错（开括号错位那一类）就按字符级合并，否则仍用空格拼、
        # 只是可能要按视觉顺序把两段前后调过来（详见 core/parser/_glyphs.py）。
        # 换行/空格/字符级合并都不新增block，不改变block_index颗粒度，下游分块/去重/
        # 追问上下文窗口都不受影响。
        head = ""
        tail = lines_chars[0] if lines_chars else []
        tail_bbox = line_bboxes[0] if line_bboxes else None
        tail_size = line_sizes[0] if line_sizes else 0.0
        for i in range(1, len(lines_chars)):
            cur, cur_bbox, cur_size = lines_chars[i], line_bboxes[i], line_sizes[i]
            if _lines_share_same_row(line_bboxes[i - 1], line_bboxes[i]):
                merged = _merge_same_row_chars(tail, cur)
                if merged is not None:
                    tail = merged
                    tail_bbox = _union_bbox(tail_bbox, cur_bbox)
                    tail_size = max(tail_size, cur_size)
                    continue
                if _reorder_same_row_lines(tail, cur):
                    tail, cur = cur, tail
                    tail_bbox, cur_bbox = cur_bbox, tail_bbox
                    tail_size, cur_size = cur_size, tail_size
                sep = " "
            else:
                # 不是同一视觉行，那就要问它是不是"被宽度顶回来的续写行"——是的话
                # 无缝拼（表格/日历单元格里的两行标题就在这里被并回去，见 _paragraphs.py），
                # 否则才是真正的换行。
                sep = line_join_separator(
                    _chars_text(tail), tail_bbox, tail_size,
                    _chars_text(cur), cur_bbox, cur_size,
                    _local_right_edge(line_bboxes, tail_bbox),
                )
                if sep is None:
                    sep = "\n"
            head += _chars_text(tail) + sep
            tail, tail_bbox, tail_size = cur, cur_bbox, cur_size
        text = head + _chars_text(tail)
        if not text:
            continue
        # 破损的PDF字体ToUnicode CMap有时会把正文汉字映射到"康熙部首"等码位上——
        # 肉眼和标准汉字无异，但会让LLM困惑（见 core/parser/_cjk_variants.py 顶部
        # docstring）。在这里、也就是文本刚拼出来的最早时机归一化，后续分块/校对/
        # 分层全程看到的都是干净文本。
        text = normalize_cjk_variants(text)
        avg_size = sum(sizes) / len(sizes) if sizes else 0.0  # 这个block的平均字号，供后续判断是否为标题用
        bbox = tuple(b["bbox"])
        last_row_bbox, last_row_size = _last_visual_row(line_bboxes, line_sizes)
        out.append({
            "text": text,
            "bbox": bbox,
            "avg_size": avg_size,
            "zone": _zone_of(bbox, height),
            # 块级续写判定（_paragraphs.py）问的是"这一块的**最后一行**有没有顶到栏最右"，
            # 用块 bbox 的 x1 会被块内更长的行顶替，实测因此把 `5. 目前的局限性` 这种
            # 小标题误判成续写行——它上面那行更长、顶到了栏右。
            "last_row_bbox": last_row_bbox,
            "last_row_size": last_row_size,
        })
    return out, width


def _union_bbox(a: tuple | None, b: tuple) -> tuple:
    """两个字框的并集。同一视觉行被拆成多个 line 合并回去时，续写判定要问的是合起来那一行。"""
    if a is None:
        return tuple(b)
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _last_visual_row(line_bboxes: list[tuple], line_sizes: list[float]) -> tuple[tuple, float]:
    """块内最后一个**视觉行**的 bbox 与字号。

    不能直接取 `line_bboxes[-1]`：PyMuPDF 会把同一视觉行误拆成多个 line（见
    `_lines_share_same_row`），只取最后一个拿到的是这一行右半截的坐标，左边界偏右、
    行宽偏窄，`_paragraphs.py` 的"满行"闸门会把真正的续写行挡掉。所以从末尾往回把
    判定为同一行的 line 并起来。
    """
    if not line_bboxes:
        return (0.0, 0.0, 0.0, 0.0), 0.0
    i = len(line_bboxes) - 1
    x0, y0, x1, y1 = line_bboxes[i]
    size = line_sizes[i]
    while i > 0 and _lines_share_same_row(line_bboxes[i - 1], line_bboxes[i]):
        i -= 1
        px0, py0, px1, py1 = line_bboxes[i]
        x0, y0, x1, y1 = min(x0, px0), min(y0, py0), max(x1, px1), max(y1, py1)
        size = max(size, line_sizes[i])
    return (x0, y0, x1, y1), size


# pages_raw: list[list[dict]]
#   外层list：多个页面，每个元素是"一页"
#   内层list：这一页里的所有文本块
#   dict：每个文本块本身，即 {"text":, "bbox":, "avg_size":, "zone":}
def _strip_headers_footers(pages_raw: list[list[dict]]) -> tuple[list[list[dict]], list[str | None]]:
    """跨页剔除重复出现的页眉/页脚文本，以及页码类文本。

    传入的是多页的原始块列表（每页一个list），因为判断"是否是页眉页脚"
    必须对比多页——单看一页无法区分"页眉"和"恰好在页面顶部的正文标题"。

    返回 (剔除后的块列表, 每页提取到的期刊页码)——后者供 Excel"文档页码"列用
    （详见 core/parser/CLAUDE.md）：命中 _NUMERIC_ZONE_RE 的页眉/页脚文本本来就
    要被剔除，顺手记下来，不是重复文本（属于跨页重复的运行页眉/刊名不算页码，
    只有"命中数字/罗马数字形状"这一支才算）。一页内若有多处命中，取第一个——多个
    候选极少见，不做优先级判断。

    **页码文本里的空白一律折成单个空格**：跨页对开刊物一个物理页印着左右两页的两个
    页码，PyMuPDF 把它们聚成一个块、文本是 `"31\\n32"`（`_NUMERIC_ZONE_RE` 的字符集
    含 `\\s`，整体匹配通过），不折叠的话 Excel"文档页码"单元格里会断成两行。
    **不在这里把它拆成两个逻辑页**——A类通道没有跨页拆分，那是另一件事
    （见 core/parser/CLAUDE.md"跨页对开版面"一节）。
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

    # 第二遍：逐页过滤，剔除"重复文本"和"纯页码/罗马数字页码"（_NUMERIC_ZONE_RE），
    # 后者顺带记作这一页的期刊页码候选
    result = []
    doc_pages: list[str | None] = []
    for page_raw in pages_raw:
        kept = []
        doc_page: str | None = None
        for blk in page_raw:
            in_zone = blk["zone"] in ("top", "bottom")
            if in_zone and blk["text"] in repeated:
                continue  # 跨页重复的页眉/页脚正文（刊名/栏目名等），不是页码
            if in_zone and _NUMERIC_ZONE_RE.match(blk["text"]):
                if doc_page is None:
                    doc_page = " ".join(blk["text"].split())
                continue  # 页码类文本
            kept.append(blk)
        result.append(kept)
        doc_pages.append(doc_page)
    return result, doc_pages


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
    """把一页里提取出来的文本块，按"正确阅读顺序"重新排列，并判断这页是单栏还是分栏。

    返回 (排好序的块列表, 'single'或'double')。mode 只有这两个取值——'double' 表示
    "分栏"（可能是两栏也可能是四栏），栏数体现在每个块的 column 字段上，不再往
    mode 里加新取值：mode 只被 ParsedDocument.layout_mode 用来做全文档级的粗粒度
    描述，多加取值会让 _majority_layout_mode 把"两栏页+四栏页"的正常刊物判成 'mixed'。
    """
    if force_layout == "single":
        boundaries: list[float] = []
    else:
        boundaries = _detect_column_boundaries(raw_blocks, width)
        if force_layout == "double" and not boundaries:
            boundaries = [width / 2]  # 强制分栏但没检测出分栏线时，退化为对半分
    mode = "double" if boundaries else "single"

    if mode == "single":
        # 单栏很简单：直接按y坐标（bbox[1]，即上边界）从上到下排序即可
        ordered = sorted(raw_blocks, key=lambda b: b["bbox"][1])
        for b in ordered:
            b["column"] = None  # 单栏没有栏位概念，统一置空，跟分栏分支保持字段结构一致
        return ordered, mode

    # ---- 分栏排序逻辑 ----
    n_cols = len(boundaries) + 1
    # 两栏沿用"左栏/右栏"这个说法；三栏以上没有直观的左右可言，改成第几栏
    labels = ["left", "right"] if n_cols == 2 else [f"col{i + 1}" for i in range(n_cols)]
    col_width_est = width / n_cols  # 估算单栏应有的宽度，用于判断块是否"跨栏"
    columns: list[list[dict]] = [[] for _ in range(n_cols)]
    spanning: list[dict] = []
    for b in raw_blocks:
        x0, _, x1, _ = b["bbox"]
        if (x1 - x0) > col_width_est * 1.4:
            # 块宽度明显超过单栏宽度（超过1.4倍），说明它横跨了多栏（如通栏标题）
            b["column"] = "span"
            spanning.append(b)
            continue
        center = (x0 + x1) / 2
        ci = bisect.bisect_right(boundaries, center)  # 中心点落在第几条分栏线右边，就属于第几栏
        b["column"] = labels[ci]
        columns[ci].append(b)

    # 每一栏、以及跨栏块，各自按y坐标（从上到下）排序
    for col in columns:
        col.sort(key=lambda b: b["bbox"][1])
    spanning.sort(key=lambda b: b["bbox"][1])

    if not spanning:
        # 没有跨栏块：阅读顺序就是"逐栏从左到右，每栏从头到尾"
        return [b for col in columns for b in col], mode

    # 有跨栏块（比如页面中间插了一个通栏标题）：需要把各栏内容和跨栏块按纵向位置
    # (y坐标)交替合并，而不是简单地"各栏全部+跨栏块"，否则跨栏标题下方本该紧跟的
    # 内容，阅读顺序上会被打乱到标题之前或太靠后
    ordered: list[dict] = []
    cursors = [0] * n_cols  # 分别指向每一栏"还没被消费"的下一个块
    for sp in spanning:
        sp_y = sp["bbox"][1]  # 这个跨栏块所在的纵向位置
        for ci, col in enumerate(columns):
            # 把这一栏里"比这个跨栏块更靠上"（y更小）的块，都先按顺序放进结果里
            while cursors[ci] < len(col) and col[cursors[ci]]["bbox"][1] < sp_y:
                ordered.append(col[cursors[ci]])
                cursors[ci] += 1
        ordered.append(sp)  # 该轮到这个跨栏块本身出场了
    # 循环结束后，把各栏里剩下的（在最后一个跨栏块下方的）内容依次追加进去
    for ci, col in enumerate(columns):
        ordered.extend(col[cursors[ci]:])
    return ordered, mode


def _finalize_native_page(
    raw_blocks: list[dict], width: float, page_no: int, force_layout: str, doc_page: str | None = None
) -> tuple[list[dict], str]:
    """把已剔除页眉页脚的原生页原始块，做完"分类block_type + 排出阅读顺序"，
    转换成和 _parse_pdf 最终期望的统一字典格式。

    doc_page 是 _strip_headers_footers 从这一页页眉/页脚提取到的期刊页码（可能为
    None），原样透传进输出字典，供 Excel"文档页码"列作为补充信息；位置描述不用它，
    一律报PDF物理页码（见 _common.py::_source_location）。
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
    # 必须在排序之后合并：判"这一块被栏宽顶回来了"要知道本栏的文字右边距，那要等每个块
    # 有了 column 字段才算得出来（见 core/parser/_paragraphs.py 顶部 docstring）。
    ordered = merge_wrapped_blocks(ordered)
    # 转换成 _parse_pdf 期望的中间字典结构（跟B类OCR通道输出格式保持一致，方便后续合并处理）
    finalized = [
        {
            "text": b["text"],
            "block_type": b["block_type"],
            "source_location": _source_location(page_no, mode, b.get("column")),
            "confidence": None,  # 原生提取的文本没有OCR置信度这一说，固定填None
            "doc_page": doc_page,
        }
        for b in ordered
    ]
    return finalized, mode
