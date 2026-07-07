"""阶段2验收测试：文档解析模块。

以两组“内容相同”的对照样本交叉验证为核心：
  - sample.pdf（A类）vs sample.docx（C类）
  - sample_single_column.pdf vs sample_double_column.pdf（B类，检验双栏阅读顺序还原）★核心
"""

import difflib
import sys
import time
from pathlib import Path

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
