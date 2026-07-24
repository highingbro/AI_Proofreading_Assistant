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

SAMPLES_DIR = Path(__file__).resolve().parent / "samples"


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
    blocks, _ = parser._run_structure(img)

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
