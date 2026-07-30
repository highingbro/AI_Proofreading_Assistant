"""阶段2验收测试：文档解析模块。

以两组“内容相同”的对照样本交叉验证为核心：
  - sample.pdf（A类）vs sample.docx（C类）
  - sample_single_column.pdf vs sample_double_column.pdf（B类，检验双栏阅读顺序还原）★核心
"""

import difflib
import sys
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.parser import NoTextLayerError, UnsupportedFormatError, parse_document

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def _normalize(text: str) -> str:
    return "".join(text.split())


def _full_text(doc) -> str:
    return _normalize("".join(b.text for b in doc.blocks))


@pytest.fixture(scope="module")
def native_pdf_doc():
    return parse_document(SAMPLES_DIR / "sample.pdf")


@pytest.fixture(scope="module")
def docx_doc():
    return parse_document(SAMPLES_DIR / "sample.docx")


@pytest.fixture(scope="module")
def single_column_doc():
    return parse_document(SAMPLES_DIR / "sample_single_column.pdf")


@pytest.fixture(scope="module")
def double_column_doc():
    return parse_document(SAMPLES_DIR / "sample_double_column.pdf")


# ---------------------------------------------------------------------------
# 回归测试：block内多行拼接必须保留换行，不能拼成空格/无分隔的连续文本
# （真实文档踩过的坑：换行丢失导致本来独立的两行被读成一句病句，或OCR场景下
# 完全无分隔地粘连成乱码，误导LLM把"解析伪影"当成"错别字/语法问题"来报）
# ---------------------------------------------------------------------------

class _FakeNativePage:
    """伪造一个具备 get_text("dict") 接口的对象，隔离测试 _extract_native_page_raw
    的拼接逻辑，不依赖真实PDF文件里恰好存在多行block。两行的bbox在y轴上完全不重叠
    （100~115 vs 118~133），代表纵向上真正独立的两行，不应被判定为同一视觉行。"""

    def get_text(self, mode):
        assert mode == "dict"
        return {
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (50.0, 100.0, 550.0, 140.0),
                    "lines": [
                        {"bbox": (50.0, 100.0, 200.0, 115.0), "spans": [{"text": "第一行文字：", "size": 12.0}]},
                        {"bbox": (50.0, 118.0, 200.0, 133.0), "spans": [{"text": "第二行文字。", "size": 12.0}]},
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


class _FakeNativePageSameRowMisplit:
    """补丁回归测试用：伪造PyMuPDF把同一视觉行误拆成两个line对象的场景——项目符号
    （符号字体，窄bbox）跟正文之间隔了一段水平间隙，但y轴范围几乎完全重叠（真实文档
    踩过的坑：Wingdings项目符号+正文被拆成两个line，中间插入换行符后LLM误判成
    "项目符号应换行"的格式问题，见 core/parser/CLAUDE.md）。"""

    def get_text(self, mode):
        assert mode == "dict"
        return {
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (90.0, 698.5, 524.4, 710.1),
                    "lines": [
                        {"bbox": (90.0, 698.5, 98.3, 710.1), "spans": [{"text": "", "size": 10.0}]},
                        {"bbox": (111.0, 698.9, 524.4, 709.4), "spans": [{"text": "系统管理员享有学生的功能", "size": 10.5}]},
                    ],
                }
            ],
        }


def test_native_pdf_same_row_misplit_lines_join_with_space():
    from core.parser.native_pdf import _extract_native_page_raw

    raw_blocks, _ = _extract_native_page_raw(_FakeNativePageSameRowMisplit())

    assert len(raw_blocks) == 1
    assert raw_blocks[0]["text"] == " 系统管理员享有学生的功能"


class _FakeNativePageBarelyOverlapping:
    """补丁回归测试用：两行y轴只有轻微擦边重叠（占较小行自身高度的比例远低于阈值），
    应仍判定为纵向上真正不同的两行，用换行符拼接，不能被误判成同一视觉行。"""

    def get_text(self, mode):
        assert mode == "dict"
        return {
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (50.0, 100.0, 550.0, 140.0),
                    "lines": [
                        {"bbox": (50.0, 100.0, 200.0, 116.0), "spans": [{"text": "第一行文字：", "size": 12.0}]},
                        {"bbox": (50.0, 115.0, 200.0, 133.0), "spans": [{"text": "第二行文字。", "size": 12.0}]},
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


class _FakeNativePageCjkVariant:
    """真实场景复现：破损字体把"工业互联网"里的"工"和"网"映射到康熙部首码位，
    验证 _extract_native_page_raw 在拼出block文本后确实做了归一化，不只是
    normalize_cjk_variants 函数本身正确。"""

    def get_text(self, mode):
        assert mode == "dict"
        return {
            "width": 600.0,
            "height": 800.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (50.0, 100.0, 550.0, 116.0),
                    "lines": [
                        {"bbox": (50.0, 100.0, 200.0, 116.0), "spans": [{"text": "⼯业互联⽹平台", "size": 12.0}]},
                    ],
                }
            ],
        }


def test_native_pdf_extract_normalizes_cjk_variants_in_block_text():
    from core.parser.native_pdf import _extract_native_page_raw

    raw_blocks, _ = _extract_native_page_raw(_FakeNativePageCjkVariant())

    assert raw_blocks[0]["text"] == "工业互联网平台"


class _FakeNativePageSpreadMisjoin:
    """补丁回归测试用：跨页对开版面（一个物理页印着左右两个页码）里，左页和右页同一
    水平线上两块**毫不相干**的文字，y轴100%重叠但横向相距半个页面，被PyMuPDF聚成了
    同一个block。坐标取自真实文档（活动手册第30/29页对开：右页最右的"联系方式"信息框
    标题 + 左页栏1的正文），只按y轴判定会把两者空格拼成一句串文喂给LLM。"""

    def get_text(self, mode):
        assert mode == "dict"
        return {
            "width": 1009.0,
            "height": 720.0,
            "blocks": [
                {
                    "type": 0,
                    "bbox": (44.5, 128.2, 821.8, 141.8),
                    "lines": [
                        {"bbox": (765.2, 128.2, 821.8, 141.8), "spans": [{"text": "联系方式", "size": 13.0}]},
                        {
                            "bbox": (44.5, 130.2, 243.0, 138.7),
                            "spans": [{"text": "造运营系统建设思路；通过课堂培训与实际演练相结", "size": 8.5}],
                        },
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
    """通栏刊头横跨整幅，仍应检出四栏（旧算法的原始误判之一，此前没有回归用例）。"""
    from core.parser._columns import _detect_column_boundaries

    blocks = _symmetric_four_column_blocks()
    blocks.append({"text": "某某期刊 2026年第8期", "bbox": (50.0, 60.0, 950.0, 75.0)})

    assert len(_detect_column_boundaries(blocks, 1000.0)) == 3


def test_columns_spread_merged_block_does_not_break_detection():
    """跨页对开版面里 PyMuPDF 把左右两页同一水平线的文字聚成的"误聚块"不得搅乱分栏。

    真实文档里这个块宽 777pt（页宽 1009）、**中心点落进左半区**，旧算法据此把左半区
    内容范围从 [44,466] 撑成整页宽 [44,953]，中部搜索带随之落到主分栏线附近而不是
    栏1|栏2 的真实间隙，四栏被判成两栏。新算法每层都用"剔通栏块之后"剩余块的范围，
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

    ★ 旧算法因"分栏线两侧字符数要平衡"必然失败，core/parser/CLAUDE.md 点名为残留未处理。
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
    blocks, _, _ = parser._run_structure(img)

    assert len(blocks) == 1
    assert blocks[0]["text"] == "第一行\n第二行"


# ---------------------------------------------------------------------------
# 双栏页期刊页码提取：A类(_strip_headers_footers)靠正则猜形状，
# B类(_run_structure)直接读版面模型自己打的"number"标签，两条通道分别验证
# ---------------------------------------------------------------------------

def test_strip_headers_footers_extracts_numeric_zone_text_as_doc_page():
    from core.parser.native_pdf import _strip_headers_footers

    pages_raw = [
        [
            {"text": "12", "zone": "bottom"},
            {"text": "正文内容", "zone": "body"},
        ]
    ]
    stripped, doc_pages = _strip_headers_footers(pages_raw)

    assert doc_pages == ["12"]
    # 页码文本本身应从保留的块里剔除，不当正文送审
    assert [b["text"] for b in stripped[0]] == ["正文内容"]


def test_strip_headers_footers_no_numeric_zone_text_returns_none():
    from core.parser.native_pdf import _strip_headers_footers

    pages_raw = [
        [
            {"text": "目录", "zone": "top"},
            {"text": "正文内容", "zone": "body"},
        ]
    ]
    stripped, doc_pages = _strip_headers_footers(pages_raw)

    assert doc_pages == [None]
    assert [b["text"] for b in stripped[0]] == ["目录", "正文内容"]


def test_strip_headers_footers_repeated_header_text_not_treated_as_doc_page():
    """跨页重复的页眉/栏目名（哪怕碰巧被判为"repeated"）不该被当成页码——
    页码逐页递增，天然不会在2页以上原样重复出现，这里只验证 repeated 分支
    和 doc_page 提取分支互不干扰。"""
    from core.parser.native_pdf import _strip_headers_footers

    pages_raw = [
        [{"text": "某某期刊", "zone": "top"}, {"text": "第一页正文", "zone": "body"}],
        [{"text": "某某期刊", "zone": "top"}, {"text": "第二页正文", "zone": "body"}],
    ]
    stripped, doc_pages = _strip_headers_footers(pages_raw)

    assert doc_pages == [None, None]
    assert [b["text"] for b in stripped[0]] == ["第一页正文"]
    assert [b["text"] for b in stripped[1]] == ["第二页正文"]


class _FakeLayoutPipelineWithPageNumber:
    def predict(self, path):
        return [
            {
                "boxes": [
                    {"label": "text", "coordinate": (0, 0, 100, 80)},
                    {"label": "number", "coordinate": (0, 90, 100, 100)},
                ]
            }
        ]


class _FakeOCRPipelineWithPageNumber:
    def predict(self, path):
        return [
            {
                "rec_texts": ["正文内容", "12"],
                "rec_scores": [0.99, 0.95],
                "rec_boxes": [(10, 10, 60, 20), (10, 92, 30, 98)],
            }
        ]


def test_ocr_run_structure_extracts_doc_page_from_number_label(monkeypatch):
    from PIL import Image

    from core.parser import ocr_pdf as parser

    monkeypatch.setattr(parser, "_get_layout_pipeline", lambda: _FakeLayoutPipelineWithPageNumber())
    monkeypatch.setattr(parser, "_get_ocr_pipeline", lambda: _FakeOCRPipelineWithPageNumber())

    img = Image.new("RGB", (100, 100), color="white")
    blocks, _, doc_page = parser._run_structure(img)

    assert doc_page == "12"
    # "number"标签区域仍要被丢弃，不进最终block列表（不是待校对正文）
    assert len(blocks) == 1
    assert blocks[0]["text"] == "正文内容"


def test_ocr_run_structure_no_number_label_returns_none_doc_page(monkeypatch):
    from PIL import Image

    from core.parser import ocr_pdf as parser

    monkeypatch.setattr(parser, "_get_layout_pipeline", lambda: _FakeLayoutPipeline())
    monkeypatch.setattr(parser, "_get_ocr_pipeline", lambda: _FakeOCRPipeline())

    img = Image.new("RGB", (100, 100), color="white")
    _, _, doc_page = parser._run_structure(img)

    assert doc_page is None


# ---------------------------------------------------------------------------
# _source_location：双栏页优先显示提取到的期刊页码，没提取到退回PDF页码；
# 单栏页完全不受影响
# ---------------------------------------------------------------------------

def test_source_location_double_column_uses_doc_page_when_present():
    from core.parser._common import _source_location

    assert _source_location(5, "double", "left", "12") == "文档第12页左栏"
    assert _source_location(5, "double", "right", "12") == "文档第12页右栏"


def test_source_location_double_column_falls_back_to_pdf_page_without_doc_page():
    from core.parser._common import _source_location

    assert _source_location(5, "double", "left", None) == "PDF第5页左栏"


def test_source_location_single_column_unaffected_by_doc_page():
    from core.parser._common import _source_location

    assert _source_location(5, "single", None, "12") == "第5页"


# ---------------------------------------------------------------------------
# 补丁回归测试：table类区域不再尝试结构识别重建，按坐标拉平的文本原样保留
# （曾用 TableRecognitionPipelineV2 重建行列结构，真实验证命中率接近零：
# 真实文档诊断发现table类区域绝大多数是说明性UI截图/菜单结构图，不是待校对
# 正文，且贡献了大量假错误，core/chunker.py 已改为整体跳过这类block不送审，
# 结构重建本身不再有必要，见 core/parser/CLAUDE.md）
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
    blocks, _, _ = parser._run_structure(img)

    assert len(blocks) == 1
    assert blocks[0]["block_type"] == "table"
    assert blocks[0]["text"] == "姓名\n年龄\n张三\n20"


# ---------------------------------------------------------------------------
# 补丁回归测试：OCR文字识别模型换档（降低低画质截图的字符误识别率）
# _get_ocr_pipeline 应把 config.PADDLEOCR_DET_MODEL/REC_MODEL 透传给 PaddleOCR
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
        parse_document(SAMPLES_DIR / "sample_single_column.pdf", ocr="off")


def test_unsupported_format(tmp_path):
    bad_file = tmp_path / "sample.txt"
    bad_file.write_text("hello", encoding="utf-8")
    with pytest.raises(UnsupportedFormatError):
        parse_document(bad_file)


def test_native_pdf_parses_faster_than_ocr(tmp_path):
    # OCR 推理耗时很高（CPU 环境下当前禁用了 MKL-DNN，见 core/parser.py 说明），
    # 只截取B类样本第1页做对比，避免重复解析整份8页文档拖慢测试。
    import fitz

    one_page_path = tmp_path / "one_page.pdf"
    src = fitz.open(SAMPLES_DIR / "sample_single_column.pdf")
    single_page_doc = fitz.open()
    single_page_doc.insert_pdf(src, from_page=0, to_page=0)
    single_page_doc.save(one_page_path)
    single_page_doc.close()
    src.close()

    t0 = time.perf_counter()
    parse_document(SAMPLES_DIR / "sample.pdf")
    native_elapsed = time.perf_counter() - t0

    t0 = time.perf_counter()
    parse_document(one_page_path)
    ocr_elapsed = time.perf_counter() - t0

    print(f"\nnative(4页)={native_elapsed:.3f}s  ocr(1页)={ocr_elapsed:.3f}s")
    assert native_elapsed < ocr_elapsed


# ---------------------------------------------------------------------------
# 交叉验证一：A类 vs C类（内容相同）
# ---------------------------------------------------------------------------

def test_cross_validate_native_pdf_vs_docx(native_pdf_doc, docx_doc):
    a = _full_text(native_pdf_doc)
    b = _full_text(docx_doc)
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    print(f"\n[交叉验证一] A类(sample.pdf) vs C类(sample.docx) 相似度: {ratio:.4f}")
    assert ratio >= 0.95, f"相似度仅 {ratio:.4f}，低于95%阈值"


# ---------------------------------------------------------------------------
# 交叉验证二（★核心）：双栏OCR vs 单栏OCR（内容相同）
# ---------------------------------------------------------------------------

def test_cross_validate_single_vs_double_column(single_column_doc, double_column_doc):
    a = _full_text(single_column_doc)
    b = _full_text(double_column_doc)
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    print(f"\n[交叉验证二★] 单栏OCR vs 双栏OCR 相似度: {ratio:.4f}")
    if ratio < 0.88:
        matcher = difflib.SequenceMatcher(None, a, b)
        diffs = [
            f"single[{i1}:{i2}]={a[i1:i2]!r} vs double[{j1}:{j2}]={b[j1:j2]!r}"
            for tag, i1, i2, j1, j2 in matcher.get_opcodes()
            if tag != "equal"
        ][:10]
        pytest.fail(f"相似度仅 {ratio:.4f}，低于88%阈值。差异样例：\n" + "\n".join(diffs))
