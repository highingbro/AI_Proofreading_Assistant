"""阶段9验收测试：原稿比对（core/comparer.py + core/workflow 编排/落库 + app.py UI冒烟）。"""

from pathlib import Path
from unittest.mock import patch

import pytest

import config
from core.comparer import build_stream, compare_documents, normalize, split_sentences
from core.parser import ParsedBlock, ParsedDocument
from core.workflow import persist_comparison_result, run_document_comparison
from db import database
from db.models import add_issue, create_record, create_task, get_issues, get_records, get_tasks


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_comparer.db"
    database.init_db(path)
    return path


def _enter_task(db_path) -> int:
    """预置一个当前任务，跳过 app.py 的任务闸门。

    app.py 是"先选任务再选功能"的线性流程——没有当前任务时只渲染任务选择界面、
    连"功能入口"radio 都不存在。这些UI冒烟测试要测的是任务之内的各个页面，所以直接
    预置 session_state["task_id"]；闸门本身由 tests/test_app_tasks.py 单独覆盖。
    """
    tasks = get_tasks(db_path=db_path)
    return tasks[0]["task_id"] if tasks else create_task("测试任务", db_path=db_path)


def _task(db_path) -> int:
    """建一个任务——records.task_id 是必填的，任何 create_record 之前都要先有任务。"""
    return create_task("测试任务", db_path=db_path)


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


def test_normalize_drops_space_between_ascii_run_and_cjk():
    # ★ 防线：判据必须只看空白紧邻的前后两个字符。若按"空白前面整个非空白串是不是
    # ASCII"判，段首的 `1997 ` 是纯数字，空格会被留下，和 Word 侧的 `1997年` 差一格。
    assert normalize("1997 年通车的广深高速") == "1997年通车的广深高速"
    assert normalize("这条收费近28 年的大动脉") == "这条收费近28年的大动脉"
    assert normalize("数字化企业网  www.e-works.net.cn") == "数字化企业网www.e-works.net.cn"


def test_normalize_keeps_space_between_latin_words():
    assert normalize("AI Agent 与 BOM") == "AI Agent与BOM"
    assert normalize("Plant\nDesign") == "Plant Design"


# ---------------------------------------------------------------------------
# build_stream / split_sentences
# ---------------------------------------------------------------------------

def test_build_stream_flattens_block_internal_newlines():
    # ★ 防线：PDF侧一个块含多段（块内\n），Word侧一段一个块，两条文本流必须一模一样
    word_side = _parsed_document([_block("第一段。", 0), _block("第二段。", 1)])
    pdf_side = _parsed_document([_block("第一段。\n第二段。", 0)])
    assert build_stream(word_side).text == build_stream(pdf_side).text == "第一段。第二段。"


def test_build_stream_joins_paragraph_split_across_blocks_seamlessly():
    # 一段话被页边界劈成两块，拼回去要和原稿的一整段完全相同
    orig = _parsed_document([_block("至此环城高速实现全线免费。", 0)])
    fmt = _parsed_document([_block("至此环", 0), _block("城高速实现全线免费。", 1, page=2)])
    assert build_stream(orig).text == build_stream(fmt).text


def test_build_stream_skips_table_blocks():
    doc = _parsed_document([_block("正文。", 0), _block("表格拉平文本", 1, block_type="table")])
    assert build_stream(doc).text == "正文。"


def test_build_stream_block_at_maps_offset_back_to_block():
    stream = build_stream(_parsed_document([_block("第一段。", 0), _block("第二段。", 1, page=2)]))
    assert stream.block_at(0).block_index == 0
    assert stream.block_at(4).block_index == 1


def test_split_sentences_keeps_punctuation_and_offsets():
    sentences, offsets = split_sentences("今天天气很好。我们去了公园。")
    assert sentences == ["今天天气很好。", "我们去了公园。"]
    assert offsets == [0, 7]


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
    assert diffs[0]["layer"] == config.LAYER_CONFIRMED


def test_compare_documents_narrows_replace_span_glued_to_punctuation_free_heading():
    # 标题不带句号，会和后一句粘成一个比对单元；报出来的范围要收窄到变了的那一句
    original = _parsed_document([_block("小标题\n它南起番禺大桥，系全国第一条城市快速路。", 0)])
    formatted = _parsed_document([_block("小标题\n它南起番禺大桥，系全国第二条城市快速路。", 0)])
    diffs = compare_documents(original, formatted)
    assert len(diffs) == 1
    assert diffs[0]["original_text"] == "系全国第一条城市快速路。"
    assert diffs[0]["formatted_text"] == "系全国第二条城市快速路。"


def test_compare_documents_layer_stays_within_the_four_layers():
    # ★ 防线：历史记录页按 config.LAYERS 四层分组渲染问题卡，四层之外的取值会让整条
    # 比对记录在历史里一张卡都渲染不出来
    original = _parsed_document([_block("第一段。", 0)])
    formatted = _parsed_document([_block("第一叚。", 0), _block("多出来的一段。", 1)])
    diffs = compare_documents(original, formatted)
    assert diffs
    assert {d["layer"] for d in diffs} == {config.LAYER_CONFIRMED}


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


def test_compare_documents_skips_table_blocks():
    original = _parsed_document(
        [_block("正文内容。", 0), _block("原稿表格", 1, block_type="table")]
    )
    formatted = _parsed_document(
        [_block("正文内容。", 0), _block("排版稿表格完全不同", 1, block_type="table")]
    )
    assert compare_documents(original, formatted) == []


def test_compare_documents_still_compares_headings():
    # ★ 防线：标题不能按块类型跳过——Word侧靠作者有没有套标题样式、PDF侧靠版面模型，
    # 两边判定标准不同，跳过就会让同一行字在一边送比、另一边不送比。
    original = _parsed_document([_block("第一章 概述。", 0, block_type="heading")])
    formatted = _parsed_document([_block("第一章 概叙。", 0, block_type="paragraph")])
    diffs = compare_documents(original, formatted)
    assert len(diffs) == 1
    assert diffs[0]["diff_type"] == "文字替换"


def test_compare_documents_ignores_block_granularity_mismatch_between_word_and_pdf():
    # ★ 防线：同内容的 Word（一段一块）与 PDF（整页一块，段落靠块内\n）必须零差异
    word_side = _parsed_document(
        [_block("今天天气很好。", 0), _block("我们去了公园。", 1), _block("1997年通车。", 2)]
    )
    pdf_side = _parsed_document([_block("今天天气很好。\n我们去了公园。\n1997 年通车。", 0)])
    assert compare_documents(word_side, pdf_side) == []


def test_compare_documents_ignores_paragraph_split_across_pages():
    # ★ 防线：排版稿把一段话劈到两页两块，不算内容差异
    original = _parsed_document([_block("至此环城高速实现全线免费。", 0)])
    formatted = _parsed_document(
        [_block("至此环", 0), _block("城高速实现全线免费。", 1, page=2)]
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
            "layer": config.LAYER_CONFIRMED,
            "suggestion": "原稿为：原文A",
        },
        {
            "page_location": "第2页",
            "block_index": None,
            "original_text": "",
            "formatted_text": "新增内容B",
            "diff_type": "新增内容",
            "layer": config.LAYER_CONFIRMED,
            "suggestion": "排版稿新增内容，原稿无对应文字",
        },
    ]

    record_id, issue_ids = persist_comparison_result(
        diffs, task_id=_task(db_path), doc_name="原稿.docx / 排版稿.pdf", db_path=db_path
    )

    assert len(issue_ids) == 2
    records = get_records(db_path=db_path)
    assert len(records) == 1
    assert records[0]["task_type"] == "原稿比对"
    assert records[0]["total_issues"] == 2
    # 比对结果全归"错误类"，记录行的分层计数要跟着填，否则历史列表那几列全是0
    assert records[0]["count_confirmed"] == 2

    issues = get_issues(record_id, db_path=db_path)
    assert len(issues) == 2
    assert issues[0]["issue_type"] == "文字替换"
    assert issues[0]["layer"] == config.LAYER_CONFIRMED
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
            "layer": config.LAYER_CONFIRMED,
            "suggestion": "原稿为：原文",
        }
    ]

    record_id, _ = persist_comparison_result(
        diffs, task_id=_task(db_path), doc_name="doc", formatted=formatted, db_path=db_path
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
        task_id=_task(db_path),
        doc_name="原稿.docx / 排版稿.pdf",
        doc_version="",
        task_type="原稿比对",
        total_issues=1,
        count_confirmed=1,
        db_path=db_path,
    )
    issue_id = add_issue(
        record_id=record_id,
        page_location="第1页",
        original_text="原文A",
        issue_type="文字替换",
        priority=config.PRIORITY_MEDIUM,
        layer=config.LAYER_CONFIRMED,
        suggestion="原稿为：原文A",
        db_path=db_path,
    )
    return record_id, issue_id


def test_app_comparison_page_initial_render_no_exception(monkeypatch, db_path):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))

    at.session_state["task_id"] = _enter_task(db_path)
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
        at.session_state["task_id"] = _enter_task(db_path)
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


def test_app_history_page_renders_comparison_issue_cards(db_path, monkeypatch):
    """★ 防线：比对结果要能在历史记录页里翻出来。

    历史记录页按 config.LAYERS 四层分组渲染问题卡，比对结果的 layer 一旦落在四层之外，
    这条记录点进去是空的——问题卡、采纳/拒绝按钮、追问全都不出现，而记录列表里还显示
    着"总数N"，看上去像数据丢了。
    """
    monkeypatch.setattr(config, "DB_PATH", db_path)
    _, issue_id = _seed_comparison_record(db_path)

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
    at.session_state["task_id"] = _enter_task(db_path)
    at.run()

    nav = next(r for r in at.radio if r.label == "功能入口")
    nav.set_value("历史记录").run()

    assert not at.exception
    assert any(b.key == f"accept_{issue_id}" for b in at.button)


def test_app_comparison_page_new_comparison_button_returns_to_upload_state(db_path, monkeypatch):
    """★ 防线：结果还在时必须留一条退回上传状态的路。

    "开始比对"按钮只在 compare_record_id 为 None 时渲染，而切走页面再回来两个
    file_uploader 会变回 None（浏览器安全限制），换文件的哈希判定就不会触发——
    没有这个按钮，页面会停在"上一次结果还在、却没有任何办法发起下一次比对"的死角。
    """
    monkeypatch.setattr(config, "DB_PATH", db_path)
    record_id, _ = _seed_comparison_record(db_path)

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
    at.session_state["task_id"] = _enter_task(db_path)
    at.session_state["compare_record_id"] = record_id
    at.run()

    nav = next(r for r in at.radio if r.label == "功能入口")
    nav.set_value("原稿比对").run()

    button = next(b for b in at.button if b.key == "new_comparison")
    button.click().run()

    assert not at.exception
    assert at.session_state["compare_record_id"] is None
    assert at.session_state["compare_file_id"] is None
    assert len(at.file_uploader) == 2


def test_app_comparison_export_does_not_trigger_rule_regeneration(db_path, tmp_path, monkeypatch):
    """★ 防线：原稿比对页的导出不触发反馈规则重算。

    规则是注入校对提示词、让LLM少报某类问题用的，只有LLM报出来的问题被拒绝才构成
    "这类判断不对"的信号。比对结果是逐字diff出来的客观差异、与LLM如何判断无关，
    总结不出任何该让LLM规避的东西，白花一次几秒的LLM调用。
    """
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "EXPORTS_DIR", tmp_path)
    record_id, _ = _seed_comparison_record(db_path)

    fake_export_path = tmp_path / "fake_export.xlsx"
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(("页码/位置", "原文"))
    wb.save(fake_export_path)

    from streamlit.testing.v1 import AppTest

    with patch("core.exporter.export_issues_to_excel", return_value=fake_export_path) as mock_export, \
         patch("core.feedback_rules.regenerate_rejection_rules") as mock_regenerate:
        at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
        at.session_state["task_id"] = _enter_task(db_path)
        at.session_state["compare_record_id"] = record_id
        at.run()

        nav = next(r for r in at.radio if r.label == "功能入口")
        nav.set_value("原稿比对").run()

        next(b for b in at.button if b.key == f"compare_export_{record_id}").click().run()

        assert not at.exception
        mock_export.assert_called_once_with(record_id)
        mock_regenerate.assert_not_called()
