"""阶段9验收测试：原稿比对（core/comparer.py + core/workflow 编排/落库 + app.py UI冒烟）。"""

from pathlib import Path
from unittest.mock import patch

import pytest

import config
from core.comparer import align_paragraphs, compare_documents, normalize, sentence_level_diff
from core.parser import ParsedBlock, ParsedDocument
from core.workflow import persist_comparison_result, run_document_comparison
from db import database
from db.models import add_issue, create_record, get_issues, get_records


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_comparer.db"
    database.init_db(path)
    return path


def _block(text, block_index, block_type="paragraph", page=1):
    return ParsedBlock(
        page=page,
        block_index=block_index,
        text=text,
        block_type=block_type,
        source_location=f"第{page}页",
    )


def _parsed_document(blocks, file_name="doc.docx", file_type="docx"):
    return ParsedDocument(file_name=file_name, file_type=file_type, total_pages=1, blocks=blocks)


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------

def test_normalize():
    assert normalize("hello\r\nworld") == "hello world"
    assert normalize("hello\rworld") == "hello world"
    assert normalize("depart-\nment") == "department"
    assert normalize("hello   world") == "hello world"
    assert normalize("你好\n世界") == "你好世界"
    assert normalize("a  b  c") == "a b c"


# ---------------------------------------------------------------------------
# align_paragraphs / pair_replace_block
# ---------------------------------------------------------------------------

def test_align_paragraphs_all_equal():
    orig = ["苹果", "香蕉", "橙子"]
    fmt = ["苹果", "香蕉", "橙子"]
    assert align_paragraphs(orig, fmt) == [(0, 0), (1, 1), (2, 2)]


def test_align_paragraphs_similar_replace_pairs_up():
    orig = ["苹果", "香蕉好吃", "橙子"]
    fmt = ["苹果", "香焦好吃", "橙子"]
    assert align_paragraphs(orig, fmt) == [(0, 0), (1, 1), (2, 2)]


def test_align_paragraphs_dissimilar_replace_becomes_delete_and_insert():
    orig = ["苹果", "香蕉", "橙子"]
    fmt = ["苹果", "西瓜", "橙子"]
    assert align_paragraphs(orig, fmt) == [(0, 0), (1, None), (None, 1), (2, 2)]


def test_align_paragraphs_unequal_length_replace_block_leftover_marked_as_insert():
    assert align_paragraphs(["a", "b", "c"], ["a", "x", "y", "c"]) == [
        (0, 0),
        (1, None),
        (None, 1),
        (None, 2),
        (2, 3),
    ]


def test_align_paragraphs_pure_insertion():
    assert align_paragraphs(["苹果"], ["苹果", "香蕉"]) == [(0, 0), (None, 1)]


def test_align_paragraphs_pure_deletion():
    assert align_paragraphs(["苹果", "香蕉"], ["苹果"]) == [(0, 0), (1, None)]


def test_align_paragraphs_replace_block_pairs_by_similarity_not_by_position():
    # orig[0]和fmt[1]几乎一样（应配对），orig[1]和fmt[0]完全不沾边（不该被位置顺序硬凑成一对）
    orig = ["我今天去了图书馆读书", "红色的自行车停在门口"]
    fmt = ["极为寒冷刺骨北风呼啸", "我今天去了图书舘读书"]
    assert align_paragraphs(orig, fmt) == [(0, 1), (None, 0), (1, None)]


# ---------------------------------------------------------------------------
# sentence_level_diff
# ---------------------------------------------------------------------------

def test_sentence_level_diff_ignores_identical_sentences():
    assert sentence_level_diff("今天天气很好。", "今天天气很好。") == []


def test_sentence_level_diff_finds_changed_sentence_only():
    orig = "今天天气很好。我们去了公园。"
    fmt = "今天天气很好。我们去了图书馆。"
    spans = sentence_level_diff(orig, fmt)
    assert len(spans) == 1
    assert spans[0]["original_text"] == "我们去了公园。"
    assert spans[0]["formatted_text"] == "我们去了图书馆。"


def test_sentence_level_diff_detects_inserted_sentence():
    orig = "第一句。"
    fmt = "第一句。第二句。"
    spans = sentence_level_diff(orig, fmt)
    assert spans == [{"change_type": "insert", "original_text": "", "formatted_text": "第二句。"}]


# ---------------------------------------------------------------------------
# compare_documents
# ---------------------------------------------------------------------------

def test_compare_documents_ignores_pure_whitespace_and_newline_differences():
    original = _parsed_document([_block("这是一段完全相同的内容。", 0)])
    formatted = _parsed_document([_block("这是一段\n完全相同的内容。", 0)])
    assert compare_documents(original, formatted) == []


def test_compare_documents_ignores_hyphenation_reflow():
    original = _parsed_document([_block("This is a reconstruction test.", 0)])
    formatted = _parsed_document([_block("This is a recon-\nstruction test.", 0)])
    assert compare_documents(original, formatted) == []


def test_compare_documents_detects_word_level_substitution():
    original = _parsed_document([_block("今天天气很好。我们去了公园。", 0)])
    formatted = _parsed_document([_block("今天天气很好。我们去了图书馆。", 0)])
    diffs = compare_documents(original, formatted)
    assert len(diffs) == 1
    assert diffs[0]["original_text"] == "我们去了公园。"
    assert diffs[0]["formatted_text"] == "我们去了图书馆。"
    assert diffs[0]["diff_type"] == "文字替换"
    assert diffs[0]["layer"] == "实质性改动"


def test_compare_documents_detects_inserted_paragraph():
    original = _parsed_document([_block("第一段。", 0)])
    formatted = _parsed_document([_block("第一段。", 0), _block("这是排版稿新增的一段。", 1)])
    diffs = compare_documents(original, formatted)
    assert len(diffs) == 1
    assert diffs[0]["diff_type"] == "新增内容"
    assert diffs[0]["formatted_text"] == "这是排版稿新增的一段。"


def test_compare_documents_detects_deleted_paragraph():
    original = _parsed_document([_block("第一段。", 0), _block("这是原稿独有、排版稿丢失的一段。", 1)])
    formatted = _parsed_document([_block("第一段。", 0)])
    diffs = compare_documents(original, formatted)
    assert len(diffs) == 1
    assert diffs[0]["diff_type"] == "删除内容"
    assert diffs[0]["original_text"] == "这是原稿独有、排版稿丢失的一段。"


def test_compare_documents_skips_non_paragraph_blocks():
    original = _parsed_document(
        [_block("标题", 0, block_type="heading"), _block("正文内容。", 1)]
    )
    formatted = _parsed_document(
        [_block("完全不同的标题", 0, block_type="heading"), _block("正文内容。", 1)]
    )
    assert compare_documents(original, formatted) == []


# ---------------------------------------------------------------------------
# core/workflow 编排/落库
# ---------------------------------------------------------------------------

def test_run_document_comparison_calls_pipeline_in_order():
    fake_original = _parsed_document([_block("原稿", 0)], file_name="original.docx")
    fake_formatted = _parsed_document([_block("排版稿", 0)], file_name="formatted.pdf")
    fake_diffs = [{"page_location": "第1页", "block_index": 0}]

    with patch(
        "core.workflow.run.parse_document", side_effect=[fake_original, fake_formatted]
    ) as mock_parse, patch(
        "core.workflow.run.compare_documents", return_value=fake_diffs
    ) as mock_compare:
        diffs, formatted_returned = run_document_comparison("original.docx", "formatted.pdf")

    assert mock_parse.call_args_list[0].args == ("original.docx",)
    assert mock_parse.call_args_list[1].args == ("formatted.pdf",)
    mock_compare.assert_called_once_with(fake_original, fake_formatted)
    assert diffs is fake_diffs
    assert formatted_returned is fake_formatted


def test_persist_comparison_result_writes_record_and_issues(db_path):
    diffs = [
        {
            "page_location": "第1页",
            "block_index": 0,
            "original_text": "原文A",
            "formatted_text": "排版稿A",
            "diff_type": "文字替换",
            "layer": "实质性改动",
            "suggestion": "原稿为：原文A",
        },
        {
            "page_location": "第2页",
            "block_index": None,
            "original_text": "",
            "formatted_text": "新增内容B",
            "diff_type": "新增内容",
            "layer": "实质性改动",
            "suggestion": "排版稿新增内容，原稿无对应文字",
        },
    ]

    record_id, issue_ids = persist_comparison_result(
        diffs, doc_name="原稿.docx / 排版稿.pdf", db_path=db_path
    )

    assert len(issue_ids) == 2
    records = get_records(db_path=db_path)
    assert len(records) == 1
    assert records[0]["task_type"] == "原稿比对"
    assert records[0]["total_issues"] == 2

    issues = get_issues(record_id, db_path=db_path)
    assert len(issues) == 2
    assert issues[0]["issue_type"] == "文字替换"
    assert issues[0]["layer"] == "实质性改动"
    assert issues[1]["issue_type"] == "新增内容"


def test_persist_comparison_result_computes_context_snippet_when_formatted_given(db_path):
    formatted = _parsed_document(
        [_block(f"第{i}段", i) for i in range(5)], file_name="formatted.pdf"
    )
    diffs = [
        {
            "page_location": "第1页",
            "block_index": 2,
            "original_text": "原文",
            "formatted_text": "排版稿文字",
            "diff_type": "文字替换",
            "layer": "实质性改动",
            "suggestion": "原稿为：原文",
        }
    ]

    record_id, _ = persist_comparison_result(
        diffs, doc_name="doc", formatted=formatted, db_path=db_path
    )

    issue = get_issues(record_id, db_path=db_path)[0]
    assert issue["context_snippet"] is not None
    assert "第2段" in issue["context_snippet"]


# ---------------------------------------------------------------------------
# app.py UI 冒烟（streamlit.testing.v1.AppTest）——与阶段6/10同一套模式：
# 打桩掉耗额度/耗时的 core.workflow 入口，真实落库到临时数据库再读回来渲染。
# ---------------------------------------------------------------------------

def _seed_comparison_record(db_path):
    record_id = create_record(
        doc_name="原稿.docx / 排版稿.pdf",
        doc_version="",
        task_type="原稿比对",
        total_issues=1,
        db_path=db_path,
    )
    issue_id = add_issue(
        record_id=record_id,
        page_location="第1页",
        original_text="原文A",
        issue_type="文字替换",
        priority=config.PRIORITY_MEDIUM,
        layer=config.DIFF_LAYER_SUBSTANTIVE,
        suggestion="原稿为：原文A",
        db_path=db_path,
    )
    return record_id, issue_id


def test_app_comparison_page_initial_render_no_exception():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
    at.run()

    nav = next(r for r in at.radio if r.label == "功能入口")
    nav.set_value("原稿比对").run()

    assert not at.exception
    assert len(at.file_uploader) == 2


def test_app_comparison_flow_with_mocked_workflow(db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path)
    record_id, issue_id = _seed_comparison_record(db_path)

    fake_formatted = _parsed_document([_block("排版稿文字", 0)], file_name="fmt.pdf")

    from streamlit.testing.v1 import AppTest

    with patch(
        "core.workflow.run_document_comparison",
        return_value=([{"page_location": "第1页", "block_index": 0}], fake_formatted),
    ) as mock_run, patch(
        "core.workflow.persist_comparison_result", return_value=(record_id, [issue_id])
    ) as mock_persist:
        at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
        at.run()

        nav = next(r for r in at.radio if r.label == "功能入口")
        nav.set_value("原稿比对").run()

        at.file_uploader[0].upload(
            "original.docx", b"dummy docx bytes",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ).run()
        at.file_uploader[1].upload("formatted.pdf", b"dummy pdf bytes", "application/pdf").run()

        start_button = next(b for b in at.button if b.label == "开始比对")
        start_button.click().run()

        assert not at.exception
        mock_run.assert_called_once()
        mock_persist.assert_called_once()
        assert at.session_state["compare_record_id"] == record_id
        assert any(b.key == f"accept_{issue_id}" for b in at.button)
