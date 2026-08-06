"""文档解析模块（core/parser/）。

绝大多数用例用构造的 PyMuPDF/python-docx 桩数据精确断言文本装配逻辑，不碰外部文件。
少数几条端到端用例要读 `samples/` 下的真实文档（四份的形态要求见 README），缺文件时由
`_sample()` 跳过——那个目录不进版本库。
"""

import sys
import time
import types
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core.parser import NoTextLayerError, UnsupportedFormatError, parse_document

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def _sample(name: str) -> Path:
    """样例文档路径；文件不在就跳过该用例，不让它 error 成"代码坏了"的样子。
    """
    path = SAMPLES_DIR / name
    if not path.exists():
        pytest.skip(f"缺少样例文档 samples/{name}（不进版本库，见 README）")
    return path


@pytest.fixture(scope="module")
def native_pdf_doc():
    return parse_document(_sample("sample.pdf"))


@pytest.fixture(scope="module")
def docx_doc():
    return parse_document(_sample("sample.docx"))


@pytest.fixture(scope="module")
def single_column_doc():
    return parse_document(_sample("sample_single_column.pdf"))


@pytest.fixture(scope="module")
def double_column_doc():
    return parse_document(_sample("sample_double_column.pdf"))


# ---------------------------------------------------------------------------
# 回归测试：block内多行拼接必须保留换行，不能拼成空格/无分隔的连续文本
# （真实文档踩过的坑：换行丢失导致本来独立的两行被读成一句病句，或OCR场景下
# 完全无分隔地粘连成乱码，误导LLM把"解析伪影"当成"错别字/语法问题"来报）
# ---------------------------------------------------------------------------

def _line(text, bbox, size):
    """按 PyMuPDF `rawdict` 的形状造一个 line：字符在 bbox 里等宽排开。

    `rawdict` 比 `dict` 多的就是每个 span 里的 `chars`（单字符 bbox）——只有它能看出
    全角开括号的墨迹偏在字框右半（见 core/parser/_glyphs.py）。等宽铺开对只关心**行级**
    拼接的用例足够；要验证字符级排序的用例（下面"全角开括号"一节）另外逐字给真实坐标。
    """
    x0, y0, x1, y1 = bbox
    step = (x1 - x0) / len(text) if text else 0.0
    chars = [{"c": c, "bbox": (x0 + i * step, y0, x0 + (i + 1) * step, y1)} for i, c in enumerate(text)]
    return {"bbox": bbox, "spans": [{"size": size, "chars": chars}]}


def _char_line(chars, size=8.5):
    """逐字给定 (字符, x0, x1) 的 line——供需要真实字符坐标的用例用，y 统一取一行的高度。"""
    y0, y1 = 522.2, 530.7
    return {
        "bbox": (min(c[1] for c in chars), y0, max(c[2] for c in chars), y1),
        "spans": [{"size": size, "chars": [{"c": c, "bbox": (x0, y0, x1, y1)} for c, x0, x1 in chars]}],
    }


class _FakeNativePageBase:
    """A类页面桩的公共父类：补上 `get_texttrace()`。

    `_extract_native_page_raw` 会调它来找"字体把字形映射成了另一个汉字"的位置（见
    core/parser/_glyph_repair.py）。这些桩测的都是文本装配逻辑、不涉及坏字形，返回空列表
    即可——真实 `fitz.Page` 一定有这个方法，桩不补就会 AttributeError。
    """

    def get_texttrace(self):
        return []


class _FakeNativePage(_FakeNativePageBase):
    """伪造一个具备 get_text("rawdict") 接口的对象，隔离测试 _extract_native_page_raw
    的拼接逻辑，不依赖真实PDF文件里恰好存在多行block。两行的bbox在y轴上完全不重叠
    （100~115 vs 118~133），代表纵向上真正独立的两行，不应被判定为同一视觉行。"""

    def get_text(self, mode):
        assert mode == "rawdict"
        return {
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (50.0, 100.0, 550.0, 140.0),
                    "lines": [
                        _line("第一行文字：", (50.0, 100.0, 200.0, 115.0), 12.0),
                        _line("第二行文字。", (50.0, 118.0, 200.0, 133.0), 12.0),
                    ],
                }
            ],
        }


def test_native_pdf_multiline_block_join_uses_newline():
    from core.parser.native_pdf import _extract_native_page_raw

    raw_blocks, width = _extract_native_page_raw(_FakeNativePage())

    assert width == 600.0
    assert len(raw_blocks) == 1
    assert raw_blocks[0]["text"] == "第一行文字：\n第二行文字。"


class _FakeNativePageSameRowMisplit(_FakeNativePageBase):
    """补丁回归测试用：伪造PyMuPDF把同一视觉行误拆成两个line对象的场景——项目符号
    （符号字体，窄bbox）跟正文之间隔了一段水平间隙，但y轴范围几乎完全重叠（真实文档
    踩过的坑：Wingdings项目符号+正文被拆成两个line，中间插入换行符后LLM误判成
    "项目符号应换行"的格式问题，见 core/parser/CLAUDE.md）。"""

    def get_text(self, mode):
        assert mode == "rawdict"
        return {
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (90.0, 698.5, 524.4, 710.1),
                    "lines": [
                        _line("", (90.0, 698.5, 98.3, 710.1), 10.0),
                        _line("系统管理员享有学生的功能", (111.0, 698.9, 524.4, 709.4), 10.5),
                    ],
                }
            ],
        }


def test_native_pdf_same_row_misplit_lines_join_with_space():
    from core.parser.native_pdf import _extract_native_page_raw

    raw_blocks, _ = _extract_native_page_raw(_FakeNativePageSameRowMisplit())

    assert len(raw_blocks) == 1
    assert raw_blocks[0]["text"] == " 系统管理员享有学生的功能"


class _FakeNativePageBarelyOverlapping(_FakeNativePageBase):
    """补丁回归测试用：两行y轴只有轻微擦边重叠（占较小行自身高度的比例远低于阈值），
    应仍判定为纵向上真正不同的两行，用换行符拼接，不能被误判成同一视觉行。"""

    def get_text(self, mode):
        assert mode == "rawdict"
        return {
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (50.0, 100.0, 550.0, 140.0),
                    "lines": [
                        _line("第一行文字：", (50.0, 100.0, 200.0, 116.0), 12.0),
                        _line("第二行文字。", (50.0, 115.0, 200.0, 133.0), 12.0),
                    ],
                }
            ],
        }


def test_native_pdf_barely_overlapping_lines_still_join_with_newline():
    from core.parser.native_pdf import _extract_native_page_raw

    raw_blocks, _ = _extract_native_page_raw(_FakeNativePageBarelyOverlapping())

    assert len(raw_blocks) == 1
    assert raw_blocks[0]["text"] == "第一行文字：\n第二行文字。"


# ---------------------------------------------------------------------------
# 被栏宽顶回来的续写块并回上一块（core/parser/_paragraphs.py）：这类期刊里
# PyMuPDF 的一个 block 常常就是一个视觉行，同一段话被栏宽切开后落在相邻的两个
# block 里，core/chunker/ 在块之间插 `\n`，LLM 就看到"…工业典型应用场\n景，助力…"
# 这种词中断行。七道闸门各挡一类实测到的误合并，下面逐条钉住。
# ---------------------------------------------------------------------------

def _wrap_block(text, bbox, size=8.0, column="col1", last_row=None):
    """构造一个已排好序、带 column 的块（`_order_native_page` 输出的形状）。"""
    return {
        "text": text,
        "bbox": bbox,
        "avg_size": size,
        "column": column,
        "zone": "body",
        "block_type": "paragraph",
        "last_row_bbox": last_row or bbox,
        "last_row_size": size,
    }


# 一栏宽 [50, 250]：满行顶到 250，正文字号 8.0，行高 10
def _full_line(text, y, **kw):
    return _wrap_block(text, (50.0, y, 250.0, y + 10.0), **kw)


def test_wrapped_blocks_same_column_continuation_merges():
    from core.parser._paragraphs import merge_wrapped_blocks

    merged = merge_wrapped_blocks([
        _full_line("讲解AI Agent关键技术与工业典型应用场", 100.0),
        _full_line("景，助力企业把握技术前沿。", 111.0),
    ])

    assert len(merged) == 1
    # 中文续写行之间不插任何分隔符
    assert merged[0]["text"] == "讲解AI Agent关键技术与工业典型应用场景，助力企业把握技术前沿。"


def test_wrapped_blocks_latin_word_boundary_gets_a_space():
    """★防线：断裂处两侧都是拉丁字母/数字时补空格，否则造出 `GEVernova` 这种连写词。"""
    from core.parser._paragraphs import merge_wrapped_blocks

    merged = merge_wrapped_blocks([
        _full_line("通用电气维尔萨公司（GE", 100.0),
        _full_line("Vernova）宣布与投资机构达成协议", 111.0),
    ])

    assert len(merged) == 1
    assert merged[0]["text"] == "通用电气维尔萨公司（GE Vernova）宣布与投资机构达成协议"


def test_wrapped_blocks_across_columns_do_not_merge():
    from core.parser._paragraphs import merge_wrapped_blocks

    merged = merge_wrapped_blocks([
        _full_line("上一栏最后一行没说完的内容还在继续排", 100.0, column="col1"),
        _full_line("下一栏开头是另一段毫不相干的文字", 111.0, column="col2"),
    ])

    assert len(merged) == 2


def test_wrapped_blocks_spanning_title_does_not_merge():
    from core.parser._paragraphs import merge_wrapped_blocks

    merged = merge_wrapped_blocks([
        _full_line("横跨整页的通栏标题没有句末标点", 100.0, column="span"),
        _full_line("下面这一段是栏内正文的开头一行内容", 111.0, column="span"),
    ])

    assert len(merged) == 2


def test_wrapped_blocks_different_font_size_do_not_merge():
    """标题字号比正文大，不该被并进下面的正文。"""
    from core.parser._paragraphs import merge_wrapped_blocks

    merged = merge_wrapped_blocks([
        _full_line("这是一个字号明显更大的小标题排满了一整行", 100.0, size=10.0),
        _full_line("这是紧接在标题下面的正文第一行内容", 111.0, size=8.0),
    ])

    assert len(merged) == 2


def test_wrapped_blocks_short_last_line_does_not_merge():
    """★防线：目录页码 `2`、竖排刊名这类又短又恰好靠右的块，必须被"满行"闸门挡住。"""
    from core.parser._paragraphs import merge_wrapped_blocks

    merged = merge_wrapped_blocks([
        _wrap_block("2", (240.0, 100.0, 250.0, 110.0)),
        _full_line("论坛计划与参会指南", 111.0),
    ])

    assert len(merged) == 2


def test_wrapped_blocks_next_starting_with_list_marker_does_not_merge():
    from core.parser._paragraphs import merge_wrapped_blocks

    merged = merge_wrapped_blocks([
        _full_line("• 华润三九：以数字化重塑中药生产全流程管控", 100.0),
        _full_line("• 格力电器：黑灯工厂的产线自动化实践", 111.0),
    ])

    assert len(merged) == 2


def test_wrapped_blocks_dot_leader_does_not_merge():
    """目录行 `标题………3` 的下一块是目录的下一条，不是它的续写。"""
    from core.parser._paragraphs import merge_wrapped_blocks

    merged = merge_wrapped_blocks([
        _full_line("（一）报名方式…………………………3", 100.0),
        _full_line("（二）进入会场的路线与停车指引说明", 111.0),
    ])

    assert len(merged) == 2


def test_wrapped_blocks_sentence_end_does_not_merge():
    from core.parser._paragraphs import merge_wrapped_blocks

    merged = merge_wrapped_blocks([
        _full_line("本次论坛的全部议程到此已经介绍完毕。", 100.0),
        _full_line("下面是完全另起一段的新内容开头一行", 111.0),
    ])

    assert len(merged) == 2


def test_wrapped_blocks_far_apart_do_not_merge():
    """纵向隔了一个段落间距/跨了版块，不是同一段。"""
    from core.parser._paragraphs import merge_wrapped_blocks

    merged = merge_wrapped_blocks([
        _full_line("上一个版块最后一行文字排满了整整一行", 100.0),
        _full_line("隔了很远的下一个版块开头的一行文字", 160.0),
    ])

    assert len(merged) == 2


def test_wrapped_blocks_judge_by_last_row_not_block_bbox():
    """★防线：判"顶到栏最右"必须用块的**最后一行**，不是块 bbox。

    真实踩坑：`5. 目前的局限性` 这种小标题，块 bbox 的 x1 被块内上面更长的行顶到了
    栏右，用块 bbox 判就会把它误判成续写行、把下面的正文并进来。
    """
    from core.parser._paragraphs import merge_wrapped_blocks

    heading = _wrap_block(
        "上面这行排满了一整行文字所以块很宽\n5. 目前的局限性",
        (50.0, 90.0, 250.0, 110.0),                 # 块 bbox 顶到了栏右 250
        last_row=(50.0, 100.0, 130.0, 110.0),       # 但最后一行只排到 130
    )
    merged = merge_wrapped_blocks([heading, _full_line("这是紧接在小标题下面的正文", 111.0)])

    assert len(merged) == 2


# ---- 块内行拼接（同一段被栏宽切开时两截落在一个block的两个line里）----

def test_wrapped_lines_same_row_cells_stay_newline():
    """★防线：表格同一行的两格（`制造业AIAgent实战落地特训营` | `合肥`）必须靠"纵向重叠"
    这道下界挡住，坐标取自真实文档（预览版PDF第3页）。

    这两格 y 只差 0.63pt、重叠 92%，横向却隔了 22 倍行高——`_lines_share_same_row` 的横向
    闸门会放行它们，而"紧邻"只判上界的话，负间距天然满足，两格就被拼成了病句。
    """
    from core.parser._paragraphs import _local_right_edge, line_join_separator

    boxes = [(112.0, 147.39, 223.4, 154.89), (392.9, 148.02, 408.2, 155.52)]
    sep = line_join_separator("制造业AIAgent实战落地特训营", boxes[0], 7.5,
                              "合肥", boxes[1], 7.5, _local_right_edge(boxes, boxes[0]))

    assert sep is None

def test_wrapped_lines_inside_block_join_seamlessly():
    """活动日历单元格里的两行标题：行0顶到本竖排最右、行1是它的续写，坐标取自真实文档。"""
    from core.parser._paragraphs import _local_right_edge, line_join_separator

    boxes = [(173.8, 137.0, 246.0, 144.5), (173.8, 145.0, 224.7, 152.5)]
    sep = line_join_separator("AI赋能设备全寿命周期", boxes[0], 7.0,
                              "管理高级研修班", boxes[1], 7.0,
                              _local_right_edge(boxes, boxes[0]))

    assert sep == ""


def test_wrapped_lines_local_right_edge_ignores_side_by_side_cell():
    """★防线：块内并排两格时，尺子必须是本格的右边距，不是整个块的最右。

    真实文档（预览版PDF第17页）有个块含左右两格共4行：右格 x∈[277,344]、左格 x∈[216,258]。
    拿块最右 344 当尺子，左格的满行永远够不着，一处也合不上。
    """
    from core.parser._paragraphs import _local_right_edge

    boxes = [
        (277.3, 526.0, 343.6, 533.0),   # 右格 行0
        (277.3, 533.5, 335.5, 540.5),   # 右格 行1
        (215.7, 519.0, 257.5, 526.0),   # 左格 行0
        (215.7, 526.5, 251.2, 533.5),   # 左格 行1
    ]

    assert _local_right_edge(boxes, boxes[2]) == 257.5   # 左格：只看左格自己
    assert _local_right_edge(boxes, boxes[0]) == 343.6   # 右格：只看右格自己


def test_wrapped_lines_short_line_inside_block_stays_newline():
    """日历里的日期数字（宽约1.2个字）顶到格子最右也不算满行，必须仍然换行。"""
    from core.parser._paragraphs import line_join_separator

    sep = line_join_separator("10", (129.4, 126.0, 137.9, 133.0), 7.0,
                              "11", (129.4, 134.0, 138.0, 141.0), 7.0, 137.9)

    assert sep is None


def test_last_visual_row_unions_misplit_lines():
    """`_last_visual_row` 要把被 PyMuPDF 误拆的同一视觉行并起来，否则末行宽度被低估。"""
    from core.parser.native_pdf import _last_visual_row

    bbox, size = _last_visual_row(
        [(50.0, 90.0, 250.0, 100.0), (50.0, 101.0, 120.0, 111.0), (125.0, 101.5, 250.0, 111.0)],
        [8.0, 8.0, 8.5],
    )

    assert bbox == (50.0, 101.0, 250.0, 111.0)
    assert size == 8.5


# ---------------------------------------------------------------------------
# 字符级装配的两件事要**每行**都做，不能只挂在"同一视觉行被拆成两个line"那条路径上：
# 按墨迹位置重排字序、清掉被相邻字符盖住的填充空格。判据都只看坐标。
# 另：编码成未映射占位码位的词间空格（`AI\x01Agent`）要还原成真空格，不能当装饰删掉。
# ---------------------------------------------------------------------------

def _char(c, x0, x1, y0=100.0, y1=110.0):
    return {"c": c, "bbox": (x0, y0, x1, y1)}


def test_single_line_block_reorders_misplaced_bracket():
    """★防线：`2《. 航空…》` 里 `.` 的字框整个落在 `《` 的空白左半，PDF流序是 `2 《 .`。

    坐标取自真实文档（预览版PDF第6页）。这一行没被 PyMuPDF 拆开，走不到
    `_merge_same_row_chars`，只有逐行排序才修得掉。
    """
    from core.parser._glyphs import _chars_text, sort_line_by_ink

    chars = [_char("2", 270.967, 275.684), _char("《", 273.586, 282.086),
             _char(".", 275.493, 277.856), _char("航", 281.915, 290.415)]

    assert _chars_text(sort_line_by_ink(chars)) == "2.《航"


def test_sort_line_by_ink_keeps_correct_line_unchanged():
    """本来就正确的 `1.《数据` 排完必须原样不动——逐行排序是新加的，这条钉住它不乱动好行。"""
    from core.parser._glyphs import _chars_text, sort_line_by_ink

    chars = [_char("1", 57.34, 62.06), _char(".", 62.44, 64.80),
             _char("《", 61.10, 69.60), _char("数", 69.60, 78.10)]

    assert _chars_text(sort_line_by_ink(chars)) == "1.《数"


def test_restore_unmapped_glyph_space_between_latin():
    """`AI Agent` 在PDF里是 `A I \\x01 A g e n t`，那个占位码位就是词间空格，删掉会变 `AIAgent`。"""
    from core.parser._glyphs import _chars_text, restore_unmapped_glyph_spaces

    span_chars = [[_char("A", 70.7, 75.9), _char("I", 76.3, 78.8)],
                  [_char("\x01", 78.8, 80.7), _char("A", 80.7, 85.8), _char("g", 86.2, 91.0)]]
    restore_unmapped_glyph_spaces(span_chars)

    assert _chars_text([c for s in span_chars for c in s]) == "AI Ag"


def test_unmapped_glyph_next_to_cjk_stays_decorative():
    """★防线：汉字旁边的占位码位是装饰图标（`\\x01活动日历`），必须仍被当装饰、照删。"""
    from core.parser._glyphs import restore_unmapped_glyph_spaces

    span_chars = [[_char("道", 10.0, 18.5), _char("\x01", 18.5, 20.4), _char("活", 20.4, 28.9)]]
    restore_unmapped_glyph_spaces(span_chars)

    assert span_chars[0][1]["c"] == "\x01"


# ---------------------------------------------------------------------------
# CJK变体字符源头归一化（core/parser/_cjk_variants.py）：破损的PDF字体ToUnicode
# CMap有时把正文汉字映射到"康熙部首"等码位上，肉眼和标准汉字无异但码位不同，
# 会让LLM产生各种困惑（详见该模块顶部docstring）。在_extract_native_page_raw
# 拼出block文本后立即归一化，下游全程只看到干净文本。
# ---------------------------------------------------------------------------

def test_normalize_cjk_variants_kangxi_radical_direct_nfkc():
    from core.parser._cjk_variants import normalize_cjk_variants

    assert normalize_cjk_variants("⼯业互联⽹") == "工业互联网"


def test_normalize_cjk_variants_kangxi_radical_traditional_fold():
    """康熙部首区NFKC分解结果是繁体字形（⼾→戶），简体正文里代表的其实是简体字，
    必须靠折回表落回"户"，不能直接采信NFKC分解结果。"""
    from core.parser._cjk_variants import normalize_cjk_variants

    assert normalize_cjk_variants("客⼾满意度") == "客户满意度"


def test_normalize_cjk_variants_radical_supplement_known_safe_entry():
    """CJK部首补充区块本身没有NFKC分解，靠人工核对过的安全子表（源自Unicode官方
    字符名，见 _cjk_variants.py）归一化，真实文档验证过的字符（⻔=门）。"""
    from core.parser._cjk_variants import normalize_cjk_variants

    assert normalize_cjk_variants("⻋⻔已锁") == "车门已锁"


def test_normalize_cjk_variants_radical_supplement_unmapped_char_untouched():
    """CJK部首补充区块里不在安全子表中的字符（如纯偏旁部首"⺅"人字旁，从未独立成字）
    原样保留，不臆测替换目标——交给分类器那层的反应式防线兜底，不在这里冒险改错内容。"""
    from core.parser._cjk_variants import normalize_cjk_variants

    assert normalize_cjk_variants("测试⺅字符") == "测试⺅字符"


def test_normalize_cjk_variants_ordinary_text_untouched():
    from core.parser._cjk_variants import normalize_cjk_variants

    text = "普通正文，不含任何变体字符。ABC123"
    assert normalize_cjk_variants(text) == text


class _FakeNativePageCjkVariant(_FakeNativePageBase):
    """真实场景复现：破损字体把"工业互联网"里的"工"和"网"映射到康熙部首码位，
    验证 _extract_native_page_raw 在拼出block文本后确实做了归一化，不只是
    normalize_cjk_variants 函数本身正确。"""

    def get_text(self, mode):
        assert mode == "rawdict"
        return {
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (50.0, 100.0, 550.0, 116.0),
                    "lines": [
                        _line("⼯业互联⽹平台", (50.0, 100.0, 200.0, 116.0), 12.0),
                    ],
                }
            ],
        }


def test_native_pdf_extract_normalizes_cjk_variants_in_block_text():
    from core.parser.native_pdf import _extract_native_page_raw

    raw_blocks, _ = _extract_native_page_raw(_FakeNativePageCjkVariant())

    assert raw_blocks[0]["text"] == "工业互联网平台"


# ---------------------------------------------------------------------------
# 字体未提供Unicode映射的占位码位（`\x01`）在源头删除
# 与上面那节同一类根因（PDF字体ToUnicode CMap有缺陷）但处置相反：那类映到了"看得懂
# 只是码位不对"的汉字要归一化，这类映不出任何字符、本身也不是文字（设计软件画的项目
# 符号/小箭头/CTA图标）要整个删掉。详见 native_pdf.py::_strip_unmapped_glyph_chars。
# ---------------------------------------------------------------------------

def test_strip_unmapped_glyph_chars_removes_control_codes():
    from core.parser.native_pdf import _strip_unmapped_glyph_chars

    assert _strip_unmapped_glyph_chars("\x01活动\x01\x01日历\x00") == "活动日历"


def test_strip_unmapped_glyph_chars_keeps_newline_separator():
    """`\\n` 是本模块拼接block内多行时自己插入的分隔符（不是PDF里的字符），必须留下——
    删掉会把本该独立的两行糊成一句病句，正是 core/parser/CLAUDE.md"block内多行拼接必须
    用换行符"一节讲的那个坑。"""
    from core.parser.native_pdf import _strip_unmapped_glyph_chars

    assert _strip_unmapped_glyph_chars("目前的局限性：\n\x01下一行内容") == "目前的局限性：\n下一行内容"


def test_strip_unmapped_glyph_chars_keeps_private_use_bullets():
    """私有使用区的符号字体项目符号（Wingdings `U+F0D8` 等）承载"这是一个列表项"的语义，
    不在删除范围内——它已由 _lines_share_same_row 用空格拼进正文，删掉反而丢信息。
    ★ 这条是防"顺手把PUA一起删了"。"""
    from core.parser.native_pdf import _strip_unmapped_glyph_chars

    assert _strip_unmapped_glyph_chars(" 系统管理员享有") == " 系统管理员享有"


def test_strip_unmapped_glyph_chars_ordinary_text_untouched():
    from core.parser.native_pdf import _strip_unmapped_glyph_chars

    text = "普通正文，含英文 APS 与数字 2026。"
    assert _strip_unmapped_glyph_chars(text) == text


class _FakeNativePageUnmappedGlyphs(_FakeNativePageBase):
    """真实场景复现：一整行只有装饰图标（`\\x01`），以及正文行里夹着图标占位码位。
    坐标让两行在y轴上完全不重叠，代表纵向真正独立的两行。"""

    def get_text(self, mode):
        assert mode == "rawdict"
        return {
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (50.0, 100.0, 550.0, 140.0),
                    "lines": [
                        _line("\x01", (50.0, 100.0, 60.0, 115.0), 12.0),
                        _line("e-works\x01简介", (50.0, 120.0, 300.0, 135.0), 12.0),
                    ],
                }
            ],
        }


def test_native_pdf_extract_strips_unmapped_glyphs_and_drops_icon_only_line():
    """整行只有装饰图标时该行整体消失（不留一个空行），正文行里的占位码位也被删净。"""
    from core.parser.native_pdf import _extract_native_page_raw

    raw_blocks, _ = _extract_native_page_raw(_FakeNativePageUnmappedGlyphs())

    assert len(raw_blocks) == 1
    assert raw_blocks[0]["text"] == "e-works简介"


# ---------------------------------------------------------------------------
# 全角开括号的墨迹只占字框右半，导致字框顺序 ≠ 视觉顺序（core/parser/_glyphs.py）
# 坐标全部取自真实文档（预览版活动计划第6页 `1.《数据治理框架及实践之道》`，字号8.5），
# 改动前拼出来是 `1《 . 数据治理框架及实践之道》`，一份刊物里几十条"书名号里多个句点"
# 的假错误全出自这里。
# ---------------------------------------------------------------------------

class _FakeNativePageOpenBracketMisorder(_FakeNativePageBase):
    """PyMuPDF 把 `1.《数据…》` 拆成 `1《` / `. 数据…》` 两个 line：`《` 字框 61.10 起、
    墨迹却约从 64.8 才开始，`.`(62.44–64.80) 整个落在它的空白左半里，`《` 与 `.` 之间
    那个 64.80–69.43 的空格是排版填充（字框几乎完全被 `《` 盖住）。"""

    def get_text(self, mode):
        assert mode == "rawdict"
        return {
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (57.3, 522.2, 175.5, 530.7),
                    "lines": [
                        _char_line([("1", 57.34, 62.06), ("《", 61.10, 69.60)]),
                        _char_line(
                            [(".", 62.44, 64.80), (" ", 64.80, 69.43)]
                            + [(c, 69.43 + i * 8.5, 77.93 + i * 8.5) for i, c in enumerate("数据治理框架及实践之道》")]
                        ),
                    ],
                }
            ],
        }


def test_native_pdf_open_bracket_order_restored():
    """★ 那几十条假错误的核心用例：句点必须落回书名号外面，填充空格一并消失。"""
    from core.parser.native_pdf import _extract_native_page_raw

    raw_blocks, _ = _extract_native_page_raw(_FakeNativePageOpenBracketMisorder())

    assert raw_blocks[0]["text"] == "1.《数据治理框架及实践之道》"


def test_ink_adjusted_x_shifts_only_opening_brackets():
    """★ 防"顺手对称处理"：闭括号/后引号的墨迹在字框左半，字框左边界与视觉位置本来就
    一致，做了修正反而会排坏。"""
    from core.parser._glyphs import _ink_adjusted_x

    box = (61.10, 522.2, 69.60, 530.7)
    assert _ink_adjusted_x({"c": "《", "bbox": box}) == 65.35
    assert _ink_adjusted_x({"c": "》", "bbox": box}) == 61.10
    assert _ink_adjusted_x({"c": "”", "bbox": box}) == 61.10
    assert _ink_adjusted_x({"c": "数", "bbox": box}) == 61.10


def test_filler_space_covered_by_neighbour_dropped_but_word_space_kept():
    """字框被邻字盖住的是排版填充，要丢；与左右邻字都不重叠的是真正的词间隔，要留。"""
    from core.parser._glyphs import _chars_text, drop_filler_spaces

    filler = [
        {"c": ".", "bbox": (62.44, 0.0, 64.80, 1.0)},
        {"c": " ", "bbox": (64.80, 0.0, 69.43, 1.0)},
        {"c": "《", "bbox": (61.10, 0.0, 69.60, 1.0)},
    ]
    assert _chars_text(drop_filler_spaces(filler)) == ".《"

    real = [
        {"c": "A", "bbox": (10.0, 0.0, 15.0, 1.0)},
        {"c": " ", "bbox": (15.0, 0.0, 19.0, 1.0)},
        {"c": "B", "bbox": (19.0, 0.0, 24.0, 1.0)},
    ]
    assert _chars_text(drop_filler_spaces(real)) == "A B"


class _FakeNativePageStackedLines(_FakeNativePageBase):
    """★ 防过度合并：79期第19页股票表格里 `*ST` 与 `⼯智` 被拆成两个 line，但两者 x 范围
    几乎完全重合（叠印在同一位置，重叠 13.7pt ≈ 两个字宽）。逐字符排会排成 `⼯*S智T`，
    按视觉顺序前后调也没有可靠依据（只差 0.79pt），应保持 PyMuPDF 原序用空格拼。"""

    def get_text(self, mode):
        assert mode == "rawdict"
        return {
            "width": 1009.0,
            "height": 720.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (707.36, 468.74, 722.26, 476.19),
                    "lines": [
                        _char_line([("*", 708.15, 711.92), ("S", 711.92, 716.57), ("T", 716.40, 721.06)]),
                        _char_line([("⼯", 707.36, 714.81), ("智", 714.81, 722.26)]),
                    ],
                }
            ],
        }


def test_native_pdf_stacked_lines_not_merged_char_by_char():
    from core.parser.native_pdf import _extract_native_page_raw

    raw_blocks, _ = _extract_native_page_raw(_FakeNativePageStackedLines())

    assert raw_blocks[0]["text"] == "*ST 工智"  # 康熙部首码位已由 _cjk_variants 归一化


class _FakeNativePageReversedRowOrder(_FakeNativePageBase):
    """真实场景复现：PyMuPDF 给的行序与视觉顺序相反——`合肥`(x393) 排在
    `9月17-18日`(x332) 前面。两段横向完全不重叠，按视觉顺序调过来用空格拼。"""

    def get_text(self, mode):
        assert mode == "rawdict"
        return {
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (332.0, 522.2, 410.0, 530.7),
                    "lines": [
                        _line("合肥", (393.0, 522.2, 410.0, 530.7), 8.5),
                        _line("9月17-18日", (332.0, 522.2, 380.0, 530.7), 8.5),
                    ],
                }
            ],
        }


def test_native_pdf_same_row_lines_reordered_by_x():
    from core.parser.native_pdf import _extract_native_page_raw

    raw_blocks, _ = _extract_native_page_raw(_FakeNativePageReversedRowOrder())

    assert raw_blocks[0]["text"] == "9月17-18日 合肥"


class _FakeNativePageAdjacentHeadings(_FakeNativePageBase):
    """★ 防"只要挨着就按字符拼"：真实文档里 `课程介绍`(574.7–603.6) 与
    `讲师介绍`(603.4–624.3) 是并排的两个表头，字框有 0.2pt 擦边重叠但并没有交错，
    按字符拼会把中间那个必要的分隔空格吃掉，变成 `课程介绍讲师介绍`。"""

    def get_text(self, mode):
        assert mode == "rawdict"
        return {
            "width": 800.0,
            "height": 800.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (574.7, 333.0, 624.3, 338.2),
                    "lines": [
                        _line("课程介绍", (574.73, 333.0, 603.62, 338.2), 5.2),
                        _line("讲师介绍", (603.42, 333.0, 624.25, 338.2), 5.2),
                    ],
                }
            ],
        }


def test_native_pdf_adjacent_headings_keep_separating_space():
    from core.parser.native_pdf import _extract_native_page_raw

    raw_blocks, _ = _extract_native_page_raw(_FakeNativePageAdjacentHeadings())

    assert raw_blocks[0]["text"] == "课程介绍 讲师介绍"


# ---------------------------------------------------------------------------
# 排版字距微调被导出成真空格：`APS`→`A PS`、`BOM`→`B O M`（core/parser/_glyphs.py）
# 判据是"空格宽度 vs 同span内字母间隙的中位数"——**不是**按字号或众数空格宽归一化，
# 那条在真实数据上证伪过（两端对齐会压缩真词间空格，与假空格完全交叠）。
# ---------------------------------------------------------------------------

def _tracked_span(text, x0=133.95, char_w=5.16, tracking=1.28, size=8.5):
    """按"整段均匀施加字距"铺出一个 span：每个字符之间（含空格位置）都隔 tracking。

    这样空格宽度恰好等于字母间隙，正是真实文档里假空格的样子。
    """
    chars = []
    x = x0
    for c in text:
        w = tracking if c == " " else char_w
        chars.append({"c": c, "bbox": (x, 0.0, x + w, 8.5)})
        x += w + (0.0 if c == " " else tracking)
    return {"size": size, "chars": chars}


def test_tracking_space_between_latin_dropped():
    """★ 用户报的那类：`A PS` 里那个 1.28pt 的空格与字母间隙同宽，是字距微调不是词间隔。"""
    from core.parser._glyphs import _chars_text, _drop_tracking_spaces

    span = _tracked_span("注重A PS应用落地深入学习")
    assert _chars_text(_drop_tracking_spaces(span["chars"])) == "注重APS应用落地深入学习"


def test_real_word_space_kept_even_when_narrow():
    """★ 防阈值调过头：两端对齐排版会把真词间空格压得很窄（`Plant Design` 只有众数宽的
    0.45），但它仍远宽于字母间隙——普通紧排文本里字母是贴着的。"""
    from core.parser._glyphs import _chars_text, _drop_tracking_spaces

    chars = []
    x = 0.0
    for c in "Featured Topic 与 Plant Design":
        w = 2.42 if c == " " else 4.8
        chars.append({"c": c, "bbox": (x, 0.0, x + w, 8.5)})
        x += w + 0.10  # 紧排：字母间隙 0.1pt，远小于 2.42 的空格
    assert _chars_text(_drop_tracking_spaces(chars)) == "Featured Topic 与 Plant Design"


def test_tracking_space_needs_latin_on_both_sides():
    """★ 汉字与URL之间那类真空格（`数字化企业网 www.e-works`，几百处）不进删除范围，
    哪怕它窄到和字母间隙同宽。"""
    from core.parser._glyphs import _chars_text, _drop_tracking_spaces

    span = _tracked_span("数字化企业网 www")
    assert _chars_text(_drop_tracking_spaces(span["chars"])) == "数字化企业网 www"


def test_tracking_space_skipped_when_too_few_gap_samples():
    """样本量不足时不判定——中位数不可靠，宁可漏修也不误删。"""
    from core.parser._glyphs import _chars_text, _drop_tracking_spaces

    span = _tracked_span("A B")  # 非空格字符对只有 0 组，够不到最小样本量
    assert _chars_text(_drop_tracking_spaces(span["chars"])) == "A B"


class _FakeNativePageTrackingSpaces(_FakeNativePageBase):
    """真实场景复现（预览版第9页 `注重APS应用落地`，字号8.5）：`A`=[133.95,139.11]、
    空格=[139.11,140.39] 宽仅 1.28pt、`P`=[140.39,145.76]、`S`=[146.95,152.03]——
    `P` 与 `S` 之间同样有 1.19pt 间隙却没有空格字符，同一行同一种间距编码得不一致。"""

    def get_text(self, mode):
        assert mode == "rawdict"
        return {
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (100.0, 100.0, 200.0, 115.0),
                    "lines": [_tracked_span_line("聚焦实施注重A PS应用落地")],
                }
            ],
        }


def _tracked_span_line(text):
    span = _tracked_span(text)
    xs = [c["bbox"] for c in span["chars"]]
    return {"bbox": (xs[0][0], 100.0, xs[-1][2], 115.0), "spans": [span]}


def test_native_pdf_extract_drops_tracking_spaces():
    from core.parser.native_pdf import _extract_native_page_raw

    raw_blocks, _ = _extract_native_page_raw(_FakeNativePageTrackingSpaces())

    assert raw_blocks[0]["text"] == "聚焦实施注重APS应用落地"


class _FakeNativePageSpreadMisjoin(_FakeNativePageBase):
    """补丁回归测试用：跨页对开版面（一个物理页印着左右两个页码）里，左页和右页同一
    水平线上两块**毫不相干**的文字，y轴100%重叠但横向相距半个页面，被PyMuPDF聚成了
    同一个block。坐标取自真实文档（活动手册第30/29页对开：右页最右的"联系方式"信息框
    标题 + 左页栏1的正文），只按y轴判定会把两者空格拼成一句串文喂给LLM。"""

    def get_text(self, mode):
        assert mode == "rawdict"
        return {
            "width": 1009.0,
            "height": 720.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (44.5, 128.2, 821.8, 141.8),
                    "lines": [
                        _line("联系方式", (765.2, 128.2, 821.8, 141.8), 13.0),
                        _line("造运营系统建设思路；通过课堂培训与实际演练相结", (44.5, 130.2, 243.0, 138.7), 8.5),
                    ],
                }
            ],
        }


def test_native_pdf_spread_far_apart_lines_join_with_newline():
    """y轴完全重叠但横向隔了半个页面（61倍行高），不能当作同一视觉行用空格拼接。"""
    from core.parser.native_pdf import _extract_native_page_raw

    raw_blocks, _ = _extract_native_page_raw(_FakeNativePageSpreadMisjoin())

    assert len(raw_blocks) == 1
    assert raw_blocks[0]["text"] == "联系方式\n造运营系统建设思路；通过课堂培训与实际演练相结"


# ---------------------------------------------------------------------------
# A类分栏检测（core/parser/_columns.py，分层占用率剖面）
#
# 全部纯构造几何、页宽统一 1000，不碰真实PDF。为什么这么算见该模块顶部 docstring；
# 每条用例都做过反向验证（把对应机制退回旧行为，确认用例真的会红）。
# ---------------------------------------------------------------------------

def _symmetric_four_column_blocks() -> list[dict]:
    """左右对称的四栏页：栏1 x50-240、栏2 x260-450、栏3 x550-740、栏4 x760-950（页宽1000）。"""
    blocks = []
    for col_x0, col_x1 in ((50.0, 240.0), (260.0, 450.0), (550.0, 740.0), (760.0, 950.0)):
        for i in range(5):
            y = 150.0 + i * 20.0
            blocks.append({"text": "正文内容占位文字正文内容占位文字", "bbox": (col_x0, y, col_x1, y + 12.0)})
    return blocks


def _dense_four_column_blocks(rows: int = 50, row_pitch: float = 12.0) -> list[dict]:
    """行数可调的对称四栏页：栏1 x50-240、栏2 x260-450、栏3 x550-740、栏4 x760-950。

    行数要能调是因为占用率是**比例**：溢出行、跨栏标题这类"堵缝"元素堵不堵死一条缝，
    取决于它占内容总高度的百分比，而不是它自己有多高。真实期刊一栏五六十行，用
    `_symmetric_four_column_blocks()` 那 5 行的版本构造不出真实的占比。
    """
    blocks = []
    for col_x0, col_x1 in ((50.0, 240.0), (260.0, 450.0), (550.0, 740.0), (760.0, 950.0)):
        for i in range(rows):
            y = 100.0 + i * row_pitch
            blocks.append({"text": "正文占位", "bbox": (col_x0, y, col_x1, y + row_pitch)})
    return blocks


def test_columns_four_column_symmetric():
    """分栏线落在三条真栏间距的中点附近（±2pt 是分箱宽度带来的量化误差，不是错误）。"""
    from core.parser._columns import _detect_column_boundaries

    lines = _detect_column_boundaries(_symmetric_four_column_blocks(), 1000.0)

    assert lines == pytest.approx([250.0, 500.0, 750.0], abs=2.0)


def test_columns_spanning_header_does_not_break_gap():
    """通栏刊头横跨整幅，仍应检出四栏。"""
    from core.parser._columns import _detect_column_boundaries

    blocks = _symmetric_four_column_blocks()
    blocks.append({"text": "某某期刊 2026年第8期", "bbox": (50.0, 60.0, 950.0, 75.0)})

    assert len(_detect_column_boundaries(blocks, 1000.0)) == 3


def test_columns_spread_merged_block_does_not_break_detection():
    """跨页对开版面里 PyMuPDF 把左右两页同一水平线的文字聚成的"误聚块"不得搅乱分栏。

    真实文档里这个块宽 777pt（页宽 1009）、**中心点落进左半区**：拿半区全部块算内容
    范围的话，会从 [44,466] 被撑成整页宽 [44,953]，搜索带随之落到主分栏线附近而不是
    栏1|栏2 的真实间隙，四栏被判成两栏。防线是每层都用"剔通栏块之后"剩余块的范围，
    这个块在任何一层都够宽、必被剔除。
    """
    from core.parser._columns import _detect_column_boundaries

    blocks = _symmetric_four_column_blocks()
    blocks.append({"text": "联系方式\n造运营系统建设思路", "bbox": (50.0, 100.0, 830.0, 115.0)})

    assert len(_detect_column_boundaries(blocks, 1000.0)) == 3


def test_columns_overflow_lines_do_not_close_gap():
    """两端对齐让少数行溢出到栏间距里，占用率加权下不该把整条缝判死。

    ★ 没有这条，本次重写的核心机制（占用率剖面取代二值覆盖图）就没有回归保护：
    二值覆盖图里只要一行溢出，整条缝的格子就全被标成"有内容"，四栏必然退回两栏。
    """
    from core.parser._columns import _detect_column_boundaries

    blocks = _dense_four_column_blocks(rows=50)
    # 栏3 有 2 行溢出到栏3|栏4 的缝里（占用率 2/50 = 0.04，低于 τ=0.06）
    for i in (7, 23):
        y = 100.0 + i * 12.0
        blocks.append({"text": "溢出行", "bbox": (550.0, y, 758.0, y + 12.0)})

    assert len(_detect_column_boundaries(blocks, 1000.0)) == 3


def test_columns_asymmetric_four_column():
    """末栏只有4行短文本（联系方式框那类）时仍应检出四栏。

    ★ 任何"分栏线两侧字符数要平衡"的判据在这里必然失败，所以定栏数一个字符都不许看。
    """
    from core.parser._columns import _detect_column_boundaries

    blocks = [b for b in _dense_four_column_blocks(rows=50) if b["bbox"][0] != 760.0]
    for i in range(4):
        y = 100.0 + i * 12.0
        blocks.append({"text": "联系方式", "bbox": (760.0, y, 950.0, y + 12.0)})

    assert len(_detect_column_boundaries(blocks, 1000.0)) == 3


def test_columns_spanning_title_uses_half_region_scale():
    """跨栏标题的通栏门限必须按半区**内容范围**宽算，不是几何区域宽（含页边距）。

    ★ 标定阶段的核心修复点，复刻真实开篇页：右半几何宽 500（主缝到页边）→ 门限 300，
    而右半内容范围只有 [550,950] 宽 400 → 门限 240；堵住栏3|栏4 缝的文章大标题宽 270
    正好卡在两者之间。按几何宽算它留在统计里、把缝占到 0.09 盖过 τ=0.06，四栏退回两栏；
    按内容范围宽算才认得出它是"这半区里的通栏块"。没这条用例，后人"顺手"改回几何宽
    只会掉几页四栏、测试全绿。
    """
    from core.parser._columns import _detect_column_boundaries

    blocks = _dense_four_column_blocks(rows=50)
    blocks.append({"text": "文章大标题跨栏排", "bbox": (620.0, 20.0, 890.0, 80.0)})

    assert len(_detect_column_boundaries(blocks, 1000.0)) == 3


def test_columns_single_column_not_split():
    """整幅宽行 + 段末短行 + 列表缩进，不该产生任何分栏线。"""
    from core.parser._columns import _detect_column_boundaries

    blocks = []
    for i in range(40):
        y = 100.0 + i * 15.0
        x1 = 950.0 if i % 5 else 520.0                  # 每5行一个段末短行
        x0 = 90.0 if i % 7 == 3 else 50.0               # 偶尔有列表缩进
        blocks.append({"text": "单栏正文占位文字", "bbox": (x0, y, x1, y + 12.0)})

    assert _detect_column_boundaries(blocks, 1000.0) == []


def test_columns_two_column_unequal_widths_accepted():
    """宽正文 + 窄边栏（2:1）是真实存在的版面，不能因栏宽不均被否决成单栏。

    ★ CV 在分层实现里只用于同层候选之间排序、从不否决，这条用例钉住这一点。

    2:1 已接近这套算法能接受的不均上限：最宽那栏一旦超过内容范围宽的
    `COLUMN_A_SPANNING_WIDTH_RATIO`(0.6) 就被当成通栏块剔除，两栏随之退回单栏。这不是
    疏漏而是同一道机制的两面——正是它让 221 页单栏文档零误判（单栏页每行本身就接近整幅
    宽度，会被整体剔除、剩下的短行凑不出栏间距），且退回单栏属安全方向。
    """
    from core.parser._columns import _detect_column_boundaries

    blocks = []
    for i in range(40):
        y = 100.0 + i * 15.0
        blocks.append({"text": "正文", "bbox": (50.0, y, 450.0, y + 12.0)})
        blocks.append({"text": "边栏", "bbox": (560.0, y, 760.0, y + 12.0)})

    assert _detect_column_boundaries(blocks, 1000.0) == pytest.approx([505.0], abs=2.0)


def test_columns_table_column_gaps_rejected():
    """上部是4列表格、下部是整幅正文：表格列间距不得被当成栏间距。

    ★ 风险 R1（等宽表格被当多栏，最高危：正文被按列切碎比漏检严重得多）的直接验证。
    这里挡住它的是主缝宽度下限——表格列间距只有 20pt，远低于
    `COLUMN_A_MAIN_GAP_MIN_WIDTH_RATIO`(页宽5% = 50pt)。反向验证：把该下限放到 0.01，
    这一页立刻被拆成四栏。

    真实文档里还有另一条防线是 τ（唯一的整页大表格样本在 τ≥0.08 时会被拆栏，故 τ 定
    0.06），但那一页的占用率分布无法用构造几何如实复刻，**只由步骤5的真实文档对比覆盖，
    不在本用例范围内**——调 τ 时不要以为这条用例绿了就安全。
    """
    from core.parser._columns import _detect_column_boundaries

    blocks = []
    for row in range(16):
        y = 100.0 + row * 12.0
        for x0, x1 in ((50.0, 270.0), (290.0, 510.0), (530.0, 750.0), (770.0, 950.0)):
            blocks.append({"text": "单元格", "bbox": (x0, y, x1, y + 12.0)})
    for row in range(24):
        y = 300.0 + row * 12.0
        blocks.append({"text": "整幅正文", "bbox": (50.0, y, 950.0, y + 12.0)})

    assert _detect_column_boundaries(blocks, 1000.0) == []


def test_columns_narrow_indent_gap_rejected():
    """8pt 的缩进空白不是栏间距，不得产生分栏线。"""
    from core.parser._columns import _detect_column_boundaries

    blocks = []
    for i in range(40):
        y = 100.0 + i * 15.0
        blocks.append({"text": "左", "bbox": (50.0, y, 492.0, y + 12.0)})
        blocks.append({"text": "右", "bbox": (500.0, y, 950.0, y + 12.0)})

    assert _detect_column_boundaries(blocks, 1000.0) == []


def test_content_x_range_uses_all_blocks():
    """内容范围取全体非空块的 min/max，**不剔通栏块**——与旧 `_content_extent` 相反。

    ★ 语义反转，容易被后人"顺手改回去"：某期刊四栏页的末栏是只有4行的联系方式框，
    整体占用率低于 τ，若再从范围里剔掉宽块，末栏宽度会算成负数。剔通栏块只发生在
    "算剖面前过滤参与统计的块"那一步。
    """
    from core.parser._columns import _content_x_range

    blocks = [
        {"text": "栏1正文", "bbox": (50.0, 150.0, 240.0, 162.0)},
        {"text": "栏2正文", "bbox": (260.0, 150.0, 450.0, 162.0)},
        {"text": "跨页误聚块", "bbox": (50.0, 100.0, 830.0, 115.0)},
    ]

    assert _content_x_range(blocks) == (50.0, 830.0)


def test_content_x_range_ignores_blank_blocks():
    from core.parser._columns import _content_x_range

    assert _content_x_range([{"text": "  \n ", "bbox": (0.0, 0.0, 900.0, 10.0)}]) is None


def test_width_cv_arithmetic():
    """CV 是给同层候选排序用的工具函数，这里只钉它自己的算术，不宣称它能挡住什么。"""
    from core.parser._columns import _width_cv

    assert _width_cv([200.0, 200.0, 200.0]) == 0.0
    assert _width_cv([100.0, 300.0]) == 0.5
    assert _width_cv([]) == float("inf")
    assert _width_cv([0.0, 0.0]) == float("inf")      # 除零保护


class _FakeLayoutPipeline:
    def predict(self, path):
        return [{"boxes": [{"label": "text", "coordinate": (0, 0, 100, 100)}]}]


class _FakeOCRPipeline:
    def predict(self, path):
        return [
            {
                "rec_texts": ["第一行", "第二行"],
                "rec_scores": [0.99, 0.98],
                "rec_boxes": [(10, 10, 60, 20), (10, 30, 60, 40)],
            }
        ]


def test_ocr_region_multiline_join_uses_newline(monkeypatch):
    from PIL import Image

    from core.parser import ocr_pdf as parser

    monkeypatch.setattr(parser, "_get_layout_pipeline", lambda: _FakeLayoutPipeline())
    monkeypatch.setattr(parser, "_get_ocr_pipeline", lambda: _FakeOCRPipeline())

    img = Image.new("RGB", (100, 100), color="white")
    blocks, _ = parser._run_structure(img)

    assert len(blocks) == 1
    assert blocks[0]["text"] == "第一行\n第二行"


# ---------------------------------------------------------------------------
# 页眉/页脚与页码类文本的剔除（刊物自己印的页码不提取，位置一律报PDF物理页码）
# ---------------------------------------------------------------------------

def test_strip_headers_footers_drops_numeric_zone_text():
    from core.parser.native_pdf import _strip_headers_footers

    stripped = _strip_headers_footers([[
        {"text": "12", "zone": "bottom"},
        {"text": "正文内容", "zone": "body"},
    ]])

    # 页码类文本不当正文送审
    assert [b["text"] for b in stripped[0]] == ["正文内容"]


def test_strip_headers_footers_keeps_non_numeric_zone_text():
    from core.parser.native_pdf import _strip_headers_footers

    stripped = _strip_headers_footers([[
        {"text": "目录", "zone": "top"},
        {"text": "正文内容", "zone": "body"},
    ]])

    assert [b["text"] for b in stripped[0]] == ["目录", "正文内容"]


def test_strip_headers_footers_drops_text_repeated_across_pages():
    """跨页重复出现在顶/底部的文本（刊名/栏目名等）判为页眉页脚，整体剔除。"""
    from core.parser.native_pdf import _strip_headers_footers

    stripped = _strip_headers_footers([
        [{"text": "某某期刊", "zone": "top"}, {"text": "第一页正文", "zone": "body"}],
        [{"text": "某某期刊", "zone": "top"}, {"text": "第二页正文", "zone": "body"}],
    ])

    assert [b["text"] for b in stripped[0]] == ["第一页正文"]
    assert [b["text"] for b in stripped[1]] == ["第二页正文"]


# ---------------------------------------------------------------------------
# _source_location：页码一律用PDF物理页码，不分单栏双栏
# ---------------------------------------------------------------------------

def test_source_location_double_column_uses_pdf_page():
    from core.parser._common import _source_location

    assert _source_location(5, "double", "left") == "第5页左栏"
    assert _source_location(5, "double", "right") == "第5页右栏"


def test_source_location_multi_column_reports_column_index():
    from core.parser._common import _source_location

    assert _source_location(5, "double", "col3") == "第5页第3栏"
    assert _source_location(5, "double", "span") == "第5页通栏"


def test_source_location_single_column_reports_page_only():
    from core.parser._common import _source_location

    assert _source_location(5, "single", None) == "第5页"


# ---------------------------------------------------------------------------
# 防线：table类区域不做结构识别重建，按坐标拉平的文本原样保留
# （这类区域绝大多数是说明性UI截图/菜单结构图、不是待校对正文，core/chunker/ 整体
# 跳过它们不送审；TableRecognitionPipelineV2 这条路已否决，见 core/parser/CLAUDE.md）
# ---------------------------------------------------------------------------

class _FakeLayoutPipelineTable:
    def predict(self, path):
        return [{"boxes": [{"label": "table", "coordinate": (0, 0, 200, 200)}]}]


class _FakeOCRPipelineTable:
    def predict(self, path):
        return [
            {
                "rec_texts": ["姓名", "年龄", "张三", "20"],
                "rec_scores": [0.99, 0.99, 0.99, 0.99],
                "rec_boxes": [(10, 10, 50, 20), (100, 10, 140, 20), (10, 30, 50, 40), (100, 30, 140, 40)],
            }
        ]


def test_table_region_keeps_flat_joined_text(monkeypatch):
    from PIL import Image

    from core.parser import ocr_pdf as parser

    monkeypatch.setattr(parser, "_get_layout_pipeline", lambda: _FakeLayoutPipelineTable())
    monkeypatch.setattr(parser, "_get_ocr_pipeline", lambda: _FakeOCRPipelineTable())

    img = Image.new("RGB", (200, 200), color="white")
    blocks, _ = parser._run_structure(img)

    assert len(blocks) == 1
    assert blocks[0]["block_type"] == "table"
    assert blocks[0]["text"] == "姓名\n年龄\n张三\n20"


# ---------------------------------------------------------------------------
# OCR识别模型换档机制：_get_ocr_pipeline 把 config.PADDLEOCR_DET_MODEL/REC_MODEL
# 透传给 PaddleOCR。两个常量默认为 None（换更大档没有准确率收益，A/B 数据见
# core/parser/CLAUDE.md），机制留着供将来重新评估，这几条钉的就是透传本身
# ---------------------------------------------------------------------------

class _FakePaddleOCR:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _call_get_ocr_pipeline_capturing_kwargs(monkeypatch, det_model, rec_model):
    import paddleocr

    import config
    from core.parser import ocr_pdf as parser

    monkeypatch.setattr(parser, "_OCR_PIPELINE", None)
    monkeypatch.setattr(parser, "_resolve_device", lambda: "cpu")
    monkeypatch.setattr(paddleocr, "PaddleOCR", _FakePaddleOCR)
    monkeypatch.setattr(config, "PADDLEOCR_DET_MODEL", det_model)
    monkeypatch.setattr(config, "PADDLEOCR_REC_MODEL", rec_model)

    pipeline = parser._get_ocr_pipeline()
    return pipeline.kwargs


def test_get_ocr_pipeline_passes_configured_model_names(monkeypatch):
    kwargs = _call_get_ocr_pipeline_capturing_kwargs(
        monkeypatch, "PP-OCRv5_server_det", "PP-OCRv5_server_rec"
    )
    assert kwargs["text_detection_model_name"] == "PP-OCRv5_server_det"
    assert kwargs["text_recognition_model_name"] == "PP-OCRv5_server_rec"


def test_get_ocr_pipeline_omits_model_names_when_none(monkeypatch):
    kwargs = _call_get_ocr_pipeline_capturing_kwargs(monkeypatch, None, None)
    assert "text_detection_model_name" not in kwargs
    assert "text_recognition_model_name" not in kwargs


# ---------------------------------------------------------------------------
# 补丁回归测试：docx内部.rels文件里指向"zip中实际不存在的部件"的断链关系记录
# （真实文档诊断：某份从PDF转换来的docx，一张图片的关系Target被写成字面意义上的
# "NULL"，python-docx打开时因KeyError整份文件都读不出来，即使Word本身能正常打开；
# 见 core/parser/docx_parser.py::_repair_dangling_relationships 模块内文档）
# ---------------------------------------------------------------------------

def _build_docx_with_dangling_relationship(tmp_path) -> Path:
    """构造一份带断链关系记录的docx：正常docx的基础上，往
    word/_rels/document.xml.rels 里插入一条Target指向不存在部件的Relationship。
    """
    import docx as docx_module

    base_path = tmp_path / "base.docx"
    doc = docx_module.Document()
    doc.add_paragraph("这是一段正常的正文内容。")
    doc.save(base_path)

    broken_path = tmp_path / "broken.docx"
    with zipfile.ZipFile(base_path) as src, zipfile.ZipFile(broken_path, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "word/_rels/document.xml.rels":
                root = ET.fromstring(data)
                broken_rel = ET.SubElement(root, "Relationship")
                broken_rel.set("Id", "rIdBroken")
                broken_rel.set(
                    "Type", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
                )
                broken_rel.set("Target", "../NULL")
                data = ET.tostring(root, encoding="UTF-8", xml_declaration=True)
            dst.writestr(item, data)
    return broken_path


def test_parse_docx_repairs_dangling_relationship_and_still_extracts_text(tmp_path):
    from core.parser.docx_parser import parse_docx

    broken_path = _build_docx_with_dangling_relationship(tmp_path)

    # 修复前：python-docx直接打不开这份文件（这里先验证问题真实存在，不是空对空断言）
    import docx as docx_module

    with pytest.raises(KeyError):
        docx_module.Document(str(broken_path))

    parsed = parse_docx(broken_path)
    assert any("这是一段正常的正文内容" in b.text for b in parsed.blocks)


def test_repair_dangling_relationships_returns_original_path_when_no_dangling_refs(tmp_path):
    """没有任何断链关系记录时，不应该做任何多余的zip重写，原样返回path。"""
    from core.parser.docx_parser import _repair_dangling_relationships

    import docx as docx_module

    clean_path = tmp_path / "clean.docx"
    doc = docx_module.Document()
    doc.add_paragraph("正常文档，没有断链")
    doc.save(clean_path)

    result = _repair_dangling_relationships(clean_path)
    assert result == clean_path


# ---------------------------------------------------------------------------
# 补丁回归测试：段落文字被 <w:sdt>（Word/WPS内容控件，常见于第三方审阅工具留下的
# 标记span）包裹时，python-docx 原生 Paragraph.text 会静默丢字
# （真实文档诊断：某份docx经WPS审阅工具处理后，其中一段文字被 tag="reviewtag_" 的
# 内容控件包住，解析结果里这段文字整体消失，导致后续送审文本出现文档里根本不存在
# 的数量不一致，LLM据此报出的"错误"实为解析层丢字的伪影；见
# core/parser/docx_parser.py::_paragraph_full_text 模块内文档）
# ---------------------------------------------------------------------------

def _build_docx_with_sdt_wrapped_text(tmp_path) -> Path:
    """构造一份段落文字被 <w:sdt> 拦腰截断的docx：模拟真实文档里审阅工具留下的内容控件。"""
    import docx as docx_module
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    doc = docx_module.Document()
    p = doc.add_paragraph()
    xml = f"""<w:p {nsdecls("w")}>
      <w:r><w:t>目前公司有四种</w:t></w:r>
      <w:sdt>
        <w:sdtPr><w:tag w:val="reviewtag_"/></w:sdtPr>
        <w:sdtContent>
          <w:r><w:t>业务类型：企业</w:t></w:r>
        </w:sdtContent>
      </w:sdt>
      <w:r><w:t>业务、厂商业务、政府业务、咨询业务。</w:t></w:r>
    </w:p>"""
    new_p = parse_xml(xml)
    p._p.getparent().replace(p._p, new_p)

    path = tmp_path / "sdt_wrapped.docx"
    doc.save(path)
    return path


def test_parse_docx_reads_text_wrapped_in_content_control(tmp_path):
    from core.parser.docx_parser import parse_docx

    path = _build_docx_with_sdt_wrapped_text(tmp_path)

    # 修复前的对照：python-docx原生 Paragraph.text 确实会漏掉sdt包裹的内容（问题真实存在）
    import docx as docx_module

    raw_text = docx_module.Document(str(path)).paragraphs[0].text
    assert "业务类型：企业" not in raw_text

    parsed = parse_docx(path)
    assert any(
        "目前公司有四种业务类型：企业业务、厂商业务、政府业务、咨询业务。" in b.text
        for b in parsed.blocks
    )


# ---------------------------------------------------------------------------
# 基础断言
# ---------------------------------------------------------------------------

def test_docx_parsing_basic(docx_doc):
    assert docx_doc.file_type == "docx"
    assert docx_doc.text_source == "native"
    assert docx_doc.layout_mode == "single"
    assert all(b.text.strip() for b in docx_doc.blocks)  # 空段落已剔除
    assert all(b.page == 0 for b in docx_doc.blocks)


def test_native_pdf_not_ocr(native_pdf_doc):
    assert native_pdf_doc.text_source == "native"
    assert native_pdf_doc.layout_mode == "single"


def test_ocr_single_column(single_column_doc):
    assert single_column_doc.text_source == "ocr"
    assert single_column_doc.layout_mode == "single"
    assert len(single_column_doc.blocks) > 0
    assert all(b.ocr_confidence is not None for b in single_column_doc.blocks)


def test_ocr_double_column(double_column_doc):
    assert double_column_doc.text_source == "ocr"
    assert double_column_doc.layout_mode == "double"
    assert len(double_column_doc.blocks) > 0


def test_ocr_off_raises_for_scanned_pdf():
    with pytest.raises(NoTextLayerError):
        parse_document(_sample("sample_single_column.pdf"), ocr="off")


def test_unsupported_format(tmp_path):
    bad_file = tmp_path / "sample.txt"
    bad_file.write_text("hello", encoding="utf-8")
    with pytest.raises(UnsupportedFormatError):
        parse_document(bad_file)


def test_native_pdf_parses_faster_than_ocr(tmp_path):
    # OCR 推理耗时很高（CPU 环境下当前禁用了 MKL-DNN，见 core/parser/ocr_pdf.py 说明），
    # 只截取B类样本第1页做对比，避免重复解析整份8页文档拖慢测试。
    import fitz

    one_page_path = tmp_path / "one_page.pdf"
    src = fitz.open(_sample("sample_single_column.pdf"))
    single_page_doc = fitz.open()
    single_page_doc.insert_pdf(src, from_page=0, to_page=0)
    single_page_doc.save(one_page_path)
    single_page_doc.close()
    src.close()

    t0 = time.perf_counter()
    parse_document(_sample("sample.pdf"))
    native_elapsed = time.perf_counter() - t0

    t0 = time.perf_counter()
    parse_document(one_page_path)
    ocr_elapsed = time.perf_counter() - t0

    print(f"\nnative={native_elapsed:.3f}s  ocr(1页)={ocr_elapsed:.3f}s")
    assert native_elapsed < ocr_elapsed


# ---------------------------------------------------------------------------
# 字体把字形映射成"另一个毫不相干的汉字"时的检测与还原（core/parser/_glyph_repair.py）
# 真实文档诊断：`2026e-works媒体服务简介` 的 ToUnicode 表把 `每`映成`嫥`、`能`映成`腉`、
# `数字化`映成`侧㶵⻉`，人眼看PDF完全正常、只有提取文字时才错，LLM据此报出的"错别字"
# 原文根本不存在（记录79 的15条确定性错误里约10条如此）
# ---------------------------------------------------------------------------

def _ch(c: str, x: float, y: float) -> dict:
    """构造一个 rawdict 风格的字符字典（只带本模块用得到的字段）。"""
    return {"c": c, "origin": (x, y), "bbox": (x, y - 10.0, x + 10.0, y)}


def test_glyph_repair_align_only_accepts_one_to_one():
    """对齐只认 1:1 的对应——伪造位置在文字层是一个字符，OCR那边也该是一个字符。
    长度不等说明这一段根本没对齐上，宁可判读不出。
    ★ 这条是防"顺手把不等长的 replace 段也映射过去"。"""
    from core.parser._glyph_repair import align_ocr_to_text

    # 等长 replace：第2个字符对上
    assert align_ocr_to_text("智腉制造", "智能制造")[1] == "能"
    # 不等长 replace（OCR多认出一个字）：该段一个都不映射
    mapping = align_ocr_to_text("智腉造", "智能制造")
    assert 1 not in mapping


def test_glyph_repair_gate_rejects_line_ocr_disagrees_elsewhere():
    """闸门：该行非伪造位置上 OCR 与文字层对不上得多，就说明这行 OCR 本身读得不准，
    伪造位取它不可信，整行判读不出。★ 这条是防"闸门被简化掉"。"""
    from core.parser._glyph_repair import repair_line_chars

    chars = [_ch(c, 10.0 + 12 * i, 50.0) for i, c in enumerate("智腉制造厂商")]
    bad = {1}
    # OCR 把其余5个字里的4个都读错了 -> 一致率 0.2，低于阈值 0.8
    assert repair_line_chars(chars, bad, "×能××××") == {}
    # 同一行，OCR 其余位置全对 -> 采纳
    assert repair_line_chars(chars, bad, "智能制造厂商") == {1: "能"}


def test_glyph_repair_ignores_control_and_private_use_chars():
    """C0 占位码位与私有使用区字符不算"被编造出来的字"：前者由 _strip_unmapped_glyph_chars
    整个删掉（是装饰图标），后者是承载列表项语义的项目符号。★ 防"顺手把它们也换成记号"。"""
    from core.parser._glyph_repair import _is_out_of_scope

    assert _is_out_of_scope("\x01") and _is_out_of_scope("\uf0d8")
    assert not _is_out_of_scope("侧") and not _is_out_of_scope("A")


def test_glyph_repair_comparable_text_skips_dropped_chars_but_keeps_indices():
    """算一致率前要按生产口径剔掉占位码位、并做康熙部首归一化，否则 `⼚`/`⼾` 会被算成
    "OCR与文字层不一致"、把一致率压低到闸门之下；同时下标必须映射回原字符列表。
    ★ 这条钉的是"剔除后下标错位"这个最容易写错的地方。"""
    from core.parser._glyph_repair import repair_line_chars

    # 文字层：`⼚`是康熙部首(U+2F1A)、`\x01`是装饰图标；伪造位在下标4的`㉀`
    chars = [_ch(c, 10.0 + 12 * i, 50.0) for i, c in enumerate("知名\x01MES⼚㉀引荐")]
    bad = {i for i, c in enumerate(chars) if c["c"] == "㉀"}
    assert repair_line_chars(chars, bad, "知名MES厂商引荐") == {bad.pop(): "商"}


def test_glyph_repair_no_ocr_text_reads_as_unreadable():
    """OCR 没返回任何文本（识别失败/整行空白）时不猜，判读不出。"""
    from core.parser._glyph_repair import repair_line_chars

    chars = [_ch(c, 10.0 + 12 * i, 50.0) for i, c in enumerate("智腉制造")]
    assert repair_line_chars(chars, {1}, "") == {}


def test_native_pdf_marks_unrepairable_fabricated_chars(monkeypatch):
    """还原不了的伪造字符落成 config.NATIVE_UNREADABLE_GLYPH_MARK，交给 core/chunker/
    整句排除；**不是直接删字符**——`智能制造`删两个字变`智制`会造出原文没有的词。"""
    from core.parser import native_pdf

    chars = [_ch(c, 10.0 + 12 * i, 50.0) for i, c in enumerate("智腉制造")]
    span_chars = [chars]
    fabricated = {(chars[1]["origin"][0], chars[1]["origin"][1])}
    # allow_ocr=False：ocr='off' 的语义是这次调用不许碰OCR，字形还原也算在内
    native_pdf._repair_fabricated_chars(None, span_chars, fabricated, None, False)
    assert "".join(c["c"] for c in chars) == "智" + config.NATIVE_UNREADABLE_GLYPH_MARK + "制造"


def test_native_pdf_repairs_fabricated_chars_from_ocr(monkeypatch):
    """能读回真身就地改掉，不留记号。"""
    from core.parser import native_pdf

    chars = [_ch(c, 10.0 + 12 * i, 50.0) for i, c in enumerate("智腉制造")]
    span_chars = [chars]
    fabricated = {(chars[1]["origin"][0], chars[1]["origin"][1])}
    monkeypatch.setattr(native_pdf, "ocr_line_text", lambda *a, **k: "智能制造")
    native_pdf._repair_fabricated_chars(None, span_chars, fabricated, object(), True)
    assert "".join(c["c"] for c in chars) == "智能制造"


def test_glyph_repair_missing_ocr_dependency_degrades_instead_of_raising(monkeypatch):
    """★ 防线：取OCR管线失败（没装 paddlepaddle、权重拉不下来、显存不足）必须降级成
    "这一行读不出来"，不能把整篇解析打掉——一份普通的有文字层PDF不该因为缺OCR依赖就解析
    不了，何况本模块有完好的降级路径（落记号→整句不送审）。"""
    from core.parser import _glyph_repair, ocr_pdf

    def _boom():
        raise ImportError("No module named 'paddle'")

    monkeypatch.setattr(ocr_pdf, "_get_ocr_pipeline", _boom)
    page_image = types.SimpleNamespace(width=1000, height=1000, crop=lambda box: None)
    assert _glyph_repair.ocr_line_text(page_image, (10.0, 40.0, 60.0, 50.0), 400) == ""


def test_native_pdf_fabricated_index_spans_whole_line_not_per_span(monkeypatch):
    """伪造位置的下标要按**整行**算：同一个词的相邻两字可能分属不同 span，
    按 span 各算各的会让比较用文本残缺、对不上。★ 防"按span分别处理"。"""
    from core.parser import native_pdf

    left = [_ch(c, 10.0 + 12 * i, 50.0) for i, c in enumerate("智腉")]
    right = [_ch(c, 34.0 + 12 * i, 50.0) for i, c in enumerate("制造")]
    fabricated = {(left[1]["origin"][0], left[1]["origin"][1])}
    monkeypatch.setattr(native_pdf, "ocr_line_text", lambda *a, **k: "智能制造")
    native_pdf._repair_fabricated_chars(None, [left, right], fabricated, object(), True)
    assert "".join(c["c"] for c in left + right) == "智能制造"
