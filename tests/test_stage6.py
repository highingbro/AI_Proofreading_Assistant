"""阶段6验收测试：Streamlit 对话界面（标准校对流）。

core/workflow.py 的编排/落库逻辑用 mock + 临时数据库精确断言，全部不联网不耗
API额度。app.py 的 UI 交互用 streamlit.testing.v1.AppTest 做冒烟，同样打桩掉
core.workflow 里耗额度的入口，不触发真实LLM调用。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core.classifier import ClassifiedIssue, ClassifiedResult
from core.chunker import ChunkedDocument
from core.parser import ParsedDocument
from core.proofreader import ProofreadResult
from core.workflow import persist_result, run_standard_proofread, set_issue_note, set_issue_status
from db import database
from db.models import get_issues, get_records


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_app.db"
    database.init_db(path)
    return path


def _classified_issue(**overrides) -> ClassifiedIssue:
    base = dict(
        original_text="示例原文",
        issue_type="标点符号问题",
        suggestion="建议修改",
        reason="示例依据",
        block_index=0,
        page_location="第1页",
        chunk_index=0,
        located=True,
        layer=config.LAYER_CONFIRMED,
        priority=config.PRIORITY_MEDIUM,
        layer_notes=["测试用例构造"],
        llm_category="normal",
        llm_confidence="high",
        original_suggestion="建议修改",
    )
    base.update(overrides)
    return ClassifiedIssue(**base)


def _classified_result_all_layers() -> ClassifiedResult:
    issues = [
        _classified_issue(
            original_text="确定性问题", layer=config.LAYER_CONFIRMED, priority=config.PRIORITY_HIGH
        ),
        _classified_issue(
            original_text="存疑问题", block_index=1, layer=config.LAYER_DOUBTFUL, priority=config.PRIORITY_MEDIUM
        ),
        _classified_issue(
            original_text="引文问题",
            block_index=2,
            layer=config.LAYER_QUOTATION,
            priority=config.PRIORITY_LOW,
            suggestion="原文照录，不建议改动。",
        ),
        _classified_issue(
            original_text="风格问题", block_index=3, layer=config.LAYER_OPTIONAL, priority=config.PRIORITY_OPTIONAL
        ),
    ]
    stats = {
        "total_issues": 4,
        "count_confirmed": 1,
        "count_doubtful": 1,
        "count_quotation": 1,
        "count_optional": 1,
        "high_priority_count": 1,
    }
    return ClassifiedResult(issues=issues, stats=stats, warnings=[])


# ---------------------------------------------------------------------------
# core/workflow.py 单元测试
# ---------------------------------------------------------------------------

def test_run_standard_proofread_calls_pipeline_in_order():
    fake_parsed = MagicMock(spec=ParsedDocument)
    fake_chunked = MagicMock(spec=ChunkedDocument)
    fake_proofread_result = MagicMock(spec=ProofreadResult)
    fake_classified = MagicMock(spec=ClassifiedResult)
    progress_calls = []

    with patch("core.workflow.run.parse_document", return_value=fake_parsed) as mock_parse, \
         patch("core.workflow.run.chunk_document", return_value=fake_chunked) as mock_chunk, \
         patch("core.workflow.run.build_glossary", return_value=[]), \
         patch("core.workflow.run.proofread_document", return_value=fake_proofread_result) as mock_proofread, \
         patch("core.workflow.run.load_learned_feedback", return_value=[]), \
         patch("core.workflow.run.classify_issues", return_value=fake_classified) as mock_classify:
        result, parsed_returned = run_standard_proofread(
            "dummy.pdf", progress_callback=lambda c, t: progress_calls.append((c, t))
        )

    mock_parse.assert_called_once_with("dummy.pdf")
    mock_chunk.assert_called_once_with(fake_parsed)
    mock_proofread.assert_called_once()
    assert mock_proofread.call_args.args[0] is fake_chunked
    assert mock_proofread.call_args.kwargs["progress_callback"] is not None
    mock_classify.assert_called_once_with(
        fake_proofread_result, fake_parsed, fake_chunked, mode=config.PROOFREAD_MODE_DEEP, learned_feedback=[]
    )
    # 阶段7起返回 (ClassifiedResult, ParsedDocument) 元组：parsed 要传给 persist_result
    # 计算 context_snippet，ParsedDocument 只在本次调用链上存在，必须一并交出去。
    assert result is fake_classified
    assert parsed_returned is fake_parsed


def test_run_standard_proofread_forwards_mode_to_proofread_document():
    fake_parsed = MagicMock(spec=ParsedDocument)
    fake_chunked = MagicMock(spec=ChunkedDocument)
    fake_proofread_result = MagicMock(spec=ProofreadResult)
    fake_classified = MagicMock(spec=ClassifiedResult)

    with patch("core.workflow.run.parse_document", return_value=fake_parsed), \
         patch("core.workflow.run.chunk_document", return_value=fake_chunked), \
         patch("core.workflow.run.build_glossary", return_value=[]), \
         patch("core.workflow.run.proofread_document", return_value=fake_proofread_result) as mock_proofread, \
         patch("core.workflow.run.load_learned_feedback", return_value=[]), \
         patch("core.workflow.run.classify_issues", return_value=fake_classified) as mock_classify:
        run_standard_proofread("dummy.pdf", mode=config.PROOFREAD_MODE_SIMPLIFIED)

    assert mock_proofread.call_args.kwargs["mode"] == config.PROOFREAD_MODE_SIMPLIFIED
    assert mock_classify.call_args.kwargs["mode"] == config.PROOFREAD_MODE_SIMPLIFIED


def test_persist_result_writes_record_and_issues_to_temp_db(db_path):
    result = _classified_result_all_layers()

    record_id, issue_ids = persist_result(result, doc_name="测试文档.pdf", db_path=db_path)

    assert len(issue_ids) == 4

    records = get_records(db_path=db_path)
    assert len(records) == 1
    record = records[0]
    assert record["record_id"] == record_id
    assert record["doc_name"] == "测试文档.pdf"
    assert record["total_issues"] == result.stats["total_issues"]
    assert record["count_confirmed"] == result.stats["count_confirmed"]
    assert record["count_doubtful"] == result.stats["count_doubtful"]
    assert record["count_quotation"] == result.stats["count_quotation"]
    assert record["count_optional"] == result.stats["count_optional"]
    assert record["high_priority_count"] == result.stats["high_priority_count"]

    issues = get_issues(record_id, db_path=db_path)
    assert len(issues) == 4
    by_text = {row["original_text"]: row for row in issues}
    assert by_text["引文问题"]["suggestion"] == "原文照录，不建议改动。"
    assert by_text["引文问题"]["layer"] == config.LAYER_QUOTATION
    # 本测试未传 parsed（阶段7新增的可选参数），context_snippet 应保持留空；
    # 传 parsed 时的填充行为见 test_stage7.py::test_persist_result_computes_context_snippet_when_parsed_given。
    # followup_history 只在阶段7 answer_followup 实际发生追问后才会被写入。
    for row in issues:
        assert row["context_snippet"] is None
        assert row["followup_history"] is None
        assert row["status"] == "待处理"

    assert set(row["issue_id"] for row in issues) == set(issue_ids)


def test_persist_result_writes_mode_to_record(db_path):
    result = _classified_result_all_layers()

    record_id, _ = persist_result(
        result, doc_name="测试文档.pdf", mode=config.PROOFREAD_MODE_SIMPLIFIED, db_path=db_path
    )

    record = get_records(db_path=db_path)[0]
    assert record["record_id"] == record_id
    assert record["mode"] == config.PROOFREAD_MODE_SIMPLIFIED


def test_set_issue_status_updates_issue_and_record_counts(db_path):
    result = _classified_result_all_layers()
    record_id, issue_ids = persist_result(result, doc_name="测试文档.pdf", db_path=db_path)

    set_issue_status(issue_ids[0], "已采纳", record_id=record_id, db_path=db_path)
    set_issue_status(issue_ids[1], "已拒绝", record_id=record_id, db_path=db_path)

    issues = {row["issue_id"]: row for row in get_issues(record_id, db_path=db_path)}
    assert issues[issue_ids[0]]["status"] == "已采纳"
    assert issues[issue_ids[1]]["status"] == "已拒绝"

    record = get_records(db_path=db_path)[0]
    assert record["accepted_count"] == 1
    assert record["rejected_count"] == 1


def test_set_issue_status_without_record_id_only_updates_issue(db_path):
    result = _classified_result_all_layers()
    record_id, issue_ids = persist_result(result, doc_name="测试文档.pdf", db_path=db_path)

    set_issue_status(issue_ids[0], "已采纳", db_path=db_path)

    issues = {row["issue_id"]: row for row in get_issues(record_id, db_path=db_path)}
    assert issues[issue_ids[0]]["status"] == "已采纳"
    record = get_records(db_path=db_path)[0]
    assert record["accepted_count"] == 0  # 未传 record_id，不重算统计


def test_set_issue_note_independent_of_status(db_path):
    """批注与采纳/拒绝状态无关：拒绝后写批注不影响status，status变化也不清空批注。"""
    result = _classified_result_all_layers()
    record_id, issue_ids = persist_result(result, doc_name="测试文档.pdf", db_path=db_path)

    set_issue_status(issue_ids[0], "已拒绝", record_id=record_id, db_path=db_path)
    set_issue_note(issue_ids[0], "个人核实过，确实需要修改", db_path=db_path)

    issues = {row["issue_id"]: row for row in get_issues(record_id, db_path=db_path)}
    assert issues[issue_ids[0]]["status"] == "已拒绝"
    assert issues[issue_ids[0]]["note"] == "个人核实过，确实需要修改"

    set_issue_status(issue_ids[0], "已采纳", record_id=record_id, db_path=db_path)
    issues = {row["issue_id"]: row for row in get_issues(record_id, db_path=db_path)}
    assert issues[issue_ids[0]]["status"] == "已采纳"
    assert issues[issue_ids[0]]["note"] == "个人核实过，确实需要修改"  # 状态变化不清空批注


# ---------------------------------------------------------------------------
# app.py UI 冒烟（streamlit.testing.v1.AppTest）
# ---------------------------------------------------------------------------

def test_app_initial_render_no_exception():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
    at.run()

    assert not at.exception
    assert len(at.file_uploader) == 1


def test_app_upload_and_classify_flow_with_mocked_workflow(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    fake_result = _classified_result_all_layers()
    fake_parsed = MagicMock(spec=ParsedDocument)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path)

    with patch("core.workflow.run_standard_proofread", return_value=(fake_result, fake_parsed)) as mock_run, \
         patch("core.workflow.persist_result", return_value=(1, [101, 102, 103, 104])) as mock_persist, \
         patch("core.workflow.set_issue_status") as mock_set_status, \
         patch("core.followup.get_followup_history", return_value=[]):
        at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
        at.run()
        assert not at.exception

        at.file_uploader[0].upload("test.pdf", b"dummy pdf bytes", "application/pdf").run()
        assert not at.exception

        start_button = next(b for b in at.button if b.label == "开始校对")
        start_button.click().run()

        assert not at.exception
        mock_run.assert_called_once()
        mock_persist.assert_called_once()
        assert at.session_state["record_id"] == 1
        assert at.session_state["issue_ids"] == [101, 102, 103, 104]
        assert at.session_state["classified_result"] is fake_result

        accept_button = next(b for b in at.button if b.key == "accept_101")
        accept_button.click().run()

        assert not at.exception
        mock_set_status.assert_called_once_with(101, "已采纳", record_id=1, db_path=None)
        assert at.session_state["issue_status"][101]["status"] == "已采纳"


def test_app_rerun_button_reclassifies_same_file_immediately(tmp_path, monkeypatch):
    """回归测试：校对完成后，"开始校对"按钮消失（classified_result非None时不再渲染），
    换文件判断又是按内容md5哈希比对，导致同一份文件校对完想再来一遍时没有任何按钮能
    触发——必须先换成别的文件再换回来才行。修复后结果展示区顶部的"重新校对本文件"
    按钮应该在 uploaded_file 还在（没离开过页面）时一次点击就直接重新触发
    run_standard_proofread，不需要用户再点一次"开始校对"、也不需要重新上传。"""
    from streamlit.testing.v1 import AppTest

    fake_result = _classified_result_all_layers()
    fake_parsed = MagicMock(spec=ParsedDocument)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path)

    with patch("core.workflow.run_standard_proofread", return_value=(fake_result, fake_parsed)) as mock_run, \
         patch("core.workflow.persist_result", return_value=(1, [101, 102, 103, 104])) as mock_persist, \
         patch("core.followup.get_followup_history", return_value=[]):
        at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
        at.run()

        at.file_uploader[0].upload("test.pdf", b"dummy pdf bytes", "application/pdf").run()
        start_button = next(b for b in at.button if b.label == "开始校对")
        start_button.click().run()
        assert not at.exception
        assert mock_run.call_count == 1

        # 校对完成后不再有"开始校对"按钮，但应该有"重新校对本文件"按钮
        assert not any(b.label == "开始校对" for b in at.button)
        rerun_button = next(b for b in at.button if b.key == "rerun_proofread")
        rerun_button.click().run()

        # uploaded_file 没变（同一次会话没离开过页面），一次点击应该直接重新执行完毕，
        # 不需要中间再出现/再点一次"开始校对"
        assert not at.exception
        assert mock_run.call_count == 2
        assert mock_persist.call_count == 2
        assert at.session_state["classified_result"] is fake_result


def test_app_rerun_button_falls_back_to_reupload_prompt_when_file_lost(tmp_path, monkeypatch):
    """uploaded_file 因切页丢失（浏览器安全限制）时，"重新校对本文件"没有文件字节
    可用，没法直接重跑，应该退回清空缓存+提示重新上传，而不是报错/静默什么都不做。"""
    from streamlit.testing.v1 import AppTest

    fake_result = _classified_result_all_layers()
    fake_parsed = MagicMock(spec=ParsedDocument)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path)

    with patch("core.workflow.run_standard_proofread", return_value=(fake_result, fake_parsed)) as mock_run, \
         patch("core.workflow.persist_result", return_value=(1, [101, 102, 103, 104])), \
         patch("core.followup.get_followup_history", return_value=[]):
        at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
        at.run()

        at.file_uploader[0].upload("test.pdf", b"dummy pdf bytes", "application/pdf").run()
        start_button = next(b for b in at.button if b.label == "开始校对")
        start_button.click().run()
        assert mock_run.call_count == 1

        at.file_uploader[0].clear().run()  # 模拟切页导致 uploaded_file 变回 None

        rerun_button = next(b for b in at.button if b.key == "rerun_proofread")
        rerun_button.click().run()

        assert not at.exception
        assert mock_run.call_count == 1  # 没有文件字节，不应该尝试重跑
        assert len(at.warning) >= 1
        assert at.session_state["classified_result"] is None


def test_app_mode_radio_selection_forwarded_to_workflow(tmp_path, monkeypatch):
    """校对模式单选框：选择"精简"后点击"开始校对"，run_standard_proofread/persist_result
    都应以 mode="精简" 被调用（而不是默认的"深度"）。"""
    from streamlit.testing.v1 import AppTest

    fake_result = _classified_result_all_layers()
    fake_parsed = MagicMock(spec=ParsedDocument)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path)

    with patch("core.workflow.run_standard_proofread", return_value=(fake_result, fake_parsed)) as mock_run, \
         patch("core.workflow.persist_result", return_value=(1, [101, 102, 103, 104])) as mock_persist, \
         patch("core.followup.get_followup_history", return_value=[]):
        at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
        at.run()

        at.file_uploader[0].upload("test.pdf", b"dummy pdf bytes", "application/pdf").run()
        assert not at.exception

        mode_radio = next(r for r in at.radio if r.label == "校对模式")
        assert list(mode_radio.options) == list(config.PROOFREAD_MODES)
        mode_radio.set_value(config.PROOFREAD_MODE_SIMPLIFIED).run()

        start_button = next(b for b in at.button if b.label == "开始校对")
        start_button.click().run()

        assert not at.exception
        assert mock_run.call_args.kwargs["mode"] == config.PROOFREAD_MODE_SIMPLIFIED
        assert mock_persist.call_args.kwargs["mode"] == config.PROOFREAD_MODE_SIMPLIFIED
        assert at.session_state["mode"] == config.PROOFREAD_MODE_SIMPLIFIED


def test_app_note_input_saves_independent_of_status(tmp_path, monkeypatch):
    """批注输入框：与采纳/拒绝完全独立，改动即通过 on_change 落库，不需要单独的保存按钮。"""
    from streamlit.testing.v1 import AppTest

    fake_result = _classified_result_all_layers()
    fake_parsed = MagicMock(spec=ParsedDocument)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path)

    with patch("core.workflow.run_standard_proofread", return_value=(fake_result, fake_parsed)), \
         patch("core.workflow.persist_result", return_value=(1, [101, 102, 103, 104])), \
         patch("core.workflow.set_issue_note") as mock_set_note, \
         patch("core.followup.get_followup_history", return_value=[]):
        at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
        at.run()

        at.file_uploader[0].upload("test.pdf", b"dummy pdf bytes", "application/pdf").run()
        start_button = next(b for b in at.button if b.label == "开始校对")
        start_button.click().run()
        assert not at.exception

        note_input = next(t for t in at.text_input if t.key == "note_101")
        note_input.set_value("这条我核实过，建议保留").run()

        assert not at.exception
        mock_set_note.assert_called_once_with(101, "这条我核实过，建议保留", db_path=None)


def test_app_cached_result_survives_file_uploader_reset(tmp_path, monkeypatch):
    """回归测试：切换到其他功能页再切回来时，浏览器会把 file_uploader 的已选文件
    重置为 None（Streamlit/浏览器的已知限制，非bug），但已校对完的结果不应因此消失。
    用 file_uploader.clear() 模拟这个"uploaded_file 变回 None"的状态。"""
    from streamlit.testing.v1 import AppTest

    fake_result = _classified_result_all_layers()
    fake_parsed = MagicMock(spec=ParsedDocument)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path)

    with patch("core.workflow.run_standard_proofread", return_value=(fake_result, fake_parsed)), \
         patch("core.workflow.persist_result", return_value=(1, [101, 102, 103, 104])), \
         patch("core.followup.get_followup_history", return_value=[]):
        at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
        at.run()
        at.file_uploader[0].upload("test.pdf", b"dummy pdf bytes", "application/pdf").run()
        start_button = next(b for b in at.button if b.label == "开始校对")
        start_button.click().run()
        assert at.session_state["classified_result"] is fake_result

        at.file_uploader[0].clear().run()

        assert not at.exception
        assert at.session_state["classified_result"] is fake_result
        assert len([b for b in at.button if b.key == "accept_101"]) == 1


def test_app_corrupted_file_shows_error_without_crashing(tmp_path, monkeypatch):
    """上传损坏/非法内容的文件：parse_document 会真实抛异常（不打桩，不耗LLM额度），
    页面必须用 st.error 兜住、不崩溃，且不应该走到 persist_result。"""
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path)

    with patch("core.workflow.persist_result") as mock_persist:
        at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
        at.run()

        at.file_uploader[0].upload("broken.pdf", b"this is not a real pdf file", "application/pdf").run()
        assert not at.exception

        start_button = next(b for b in at.button if b.label == "开始校对")
        start_button.click().run()

        assert not at.exception  # 异常必须被 app.py 捕获展示，不能冒泡成页面崩溃
        assert len(at.error) >= 1
        mock_persist.assert_not_called()
        assert at.session_state["classified_result"] is None
