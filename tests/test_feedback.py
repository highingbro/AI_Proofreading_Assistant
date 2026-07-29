"""阶段12验收测试：人工反馈学习（db/database.py新增feedback/feedback_rules表、
db/models.py新增CRUD、core/feedback.py、core/feedback_rules.py、core/workflow/run.py
接入、app.py拒绝按钮记录反馈+反馈学习管理页）。

不耗API额度：全部用临时数据库+monkeypatch，不调用真实LLM。

补丁：原规则J（字符串相似度自动降级）已删除，本文件里原本测试
load_learned_feedback/count_similar_rejections/cluster_feedback 的三节一并删除，
改为测试 core/feedback_rules.py 的LLM语义总结（regenerate_rejection_rules）+
纯DB读取（load_rejection_rules_text），详见 core/feedback_rules.py 模块docstring。
core/classifier 相关的归层判定测试历来在 tests/test_classifier.py（本文件不涉及）。
"""

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import core.feedback_rules as feedback_rules
from core.chunker import ChunkedDocument
from core.classifier import ClassifiedResult
from core.feedback import forget_feedback, record_rejection
from core.llm_client import LLMCallError
from core.parser import ParsedDocument
from core.proofreader import ProofreadResult
from core.workflow.run import run_standard_proofread
from db import database
from db.models import add_feedback, create_task, delete_feedback, get_feedback, get_feedback_rules, get_tasks, replace_feedback_rules


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_app.db"
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


_PATH_SEP_SUGGESTION = "将'一'改为'-'或'>'等规范的路径分隔符"


# ---------------------------------------------------------------------------
# db/database.py + db/models.py：feedback 表建表与CRUD往返
# ---------------------------------------------------------------------------

def _table_columns(db_path, table_name):
    conn = database.get_connection(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {row["name"] for row in rows}
    finally:
        conn.close()


def test_feedback_table_created_with_expected_columns(db_path):
    columns = _table_columns(db_path, "feedback")
    expected = {
        "feedback_id", "created_at", "issue_type", "original_text",
        "suggestion", "reason", "source_issue_id", "source_record_id",
    }
    assert expected.issubset(columns)


def test_add_get_delete_feedback_round_trip(db_path):
    feedback_id = add_feedback(
        issue_type="错别字与拼写",
        original_text="管理控台一组织权限一用户管理",
        suggestion=_PATH_SEP_SUGGESTION,
        reason="疑似标点误用",
        source_issue_id=101,
        source_record_id=1,
        db_path=db_path,
    )
    assert feedback_id > 0

    rows = get_feedback(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["issue_type"] == "错别字与拼写"
    assert rows[0]["suggestion"] == _PATH_SEP_SUGGESTION
    assert rows[0]["reason"] == "疑似标点误用"

    filtered = get_feedback(issue_type="错别字与拼写", db_path=db_path)
    assert len(filtered) == 1
    assert get_feedback(issue_type="不存在的类型", db_path=db_path) == []

    delete_feedback(feedback_id, db_path=db_path)
    assert get_feedback(db_path=db_path) == []


# ---------------------------------------------------------------------------
# db/database.py + db/models.py：feedback_rules 表建表与CRUD往返
# ---------------------------------------------------------------------------

def test_feedback_rules_table_created_with_expected_columns(db_path):
    columns = _table_columns(db_path, "feedback_rules")
    expected = {"rule_id", "rule_text", "matched_feedback_ids", "created_at"}
    assert expected.issubset(columns)


def test_replace_feedback_rules_overwrites_previous_content(db_path):
    replace_feedback_rules(
        [{"rule_text": "旧规则", "matched_feedback_ids": [1, 2, 3]}], db_path=db_path
    )
    assert len(get_feedback_rules(db_path=db_path)) == 1

    replace_feedback_rules(
        [
            {"rule_text": "新规则一", "matched_feedback_ids": [4, 5, 6]},
            {"rule_text": "新规则二", "matched_feedback_ids": [7, 8, 9]},
        ],
        db_path=db_path,
    )
    rows = get_feedback_rules(db_path=db_path)
    assert len(rows) == 2
    assert {r["rule_text"] for r in rows} == {"新规则一", "新规则二"}
    assert rows[0]["matched_feedback_ids"] == [4, 5, 6]


def test_replace_feedback_rules_with_empty_list_clears_table(db_path):
    replace_feedback_rules([{"rule_text": "规则", "matched_feedback_ids": [1, 2, 3]}], db_path=db_path)
    replace_feedback_rules([], db_path=db_path)
    assert get_feedback_rules(db_path=db_path) == []


# ---------------------------------------------------------------------------
# core/feedback.py：record_rejection / forget_feedback
# ---------------------------------------------------------------------------

def test_record_rejection_from_live_flow_issue_with_reason(db_path):
    """实时校对流程的issue对象（ClassifiedIssue风格）带reason属性，应完整记录。"""
    issue = types.SimpleNamespace(
        issue_type="错别字与拼写",
        original_text="管理控台一组织权限一用户管理",
        suggestion=_PATH_SEP_SUGGESTION,
        reason="疑似标点误用",
    )
    record_rejection(issue, issue_id=101, record_id=1, db_path=db_path)

    rows = get_feedback(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["reason"] == "疑似标点误用"
    assert rows[0]["source_issue_id"] == 101
    assert rows[0]["source_record_id"] == 1


def test_record_rejection_from_history_page_issue_without_reason(db_path):
    """历史记录页复用的issue对象（_row_to_issue_view包装）没有reason属性，
    应退化为空字符串，不报错，suggestion/original_text/issue_type仍完整记录。"""
    issue = types.SimpleNamespace(
        issue_type="错别字与拼写",
        original_text="管理控台一组织权限一用户管理",
        suggestion=_PATH_SEP_SUGGESTION,
    )
    record_rejection(issue, issue_id=202, record_id=2, db_path=db_path)

    rows = get_feedback(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["reason"] == ""
    assert rows[0]["suggestion"] == _PATH_SEP_SUGGESTION


def test_forget_feedback_removes_entry(db_path):
    feedback_id = add_feedback(
        issue_type="错别字与拼写", original_text="原文", suggestion=_PATH_SEP_SUGGESTION, db_path=db_path,
    )
    assert len(get_feedback(db_path=db_path)) == 1

    forget_feedback(feedback_id, db_path=db_path)

    assert get_feedback(db_path=db_path) == []


# ---------------------------------------------------------------------------
# core/feedback_rules.py：regenerate_rejection_rules
# ---------------------------------------------------------------------------

def _seed_feedback_rows(db_path, n, issue_type="错别字与拼写", original_text="原文", suggestion="建议"):
    ids = []
    for i in range(n):
        ids.append(
            add_feedback(
                issue_type=issue_type, original_text=f"{original_text}{i}",
                suggestion=f"{suggestion}{i}", db_path=db_path,
            )
        )
    return ids


def test_regenerate_rejection_rules_skips_llm_call_below_threshold(db_path):
    _seed_feedback_rows(db_path, config.FEEDBACK_REJECTION_THRESHOLD - 1)
    mock_chat = MagicMock()

    with patch("core.feedback_rules.chat_completion", mock_chat):
        feedback_rules.regenerate_rejection_rules(db_path=db_path)

    mock_chat.assert_not_called()
    assert get_feedback_rules(db_path=db_path) == []


def test_regenerate_rejection_rules_filters_out_factual_issue_type(db_path):
    """全是事实类反馈时凑不够阈值（被过滤掉），不应调用LLM——即使总行数达标。"""
    _seed_feedback_rows(db_path, config.FEEDBACK_REJECTION_THRESHOLD, issue_type="常识与事实性错误")
    mock_chat = MagicMock()

    with patch("core.feedback_rules.chat_completion", mock_chat):
        feedback_rules.regenerate_rejection_rules(db_path=db_path)

    mock_chat.assert_not_called()


def test_regenerate_rejection_rules_stores_valid_llm_output(db_path):
    ids = _seed_feedback_rows(db_path, config.FEEDBACK_REJECTION_THRESHOLD)

    def fake_chat_completion(system_prompt, user_content):
        return json.dumps(
            [{"rule": "这是一条总结出的规则", "matched_feedback_ids": ids}],
            ensure_ascii=False,
        )

    with patch("core.feedback_rules.chat_completion", fake_chat_completion):
        feedback_rules.regenerate_rejection_rules(db_path=db_path)

    rows = get_feedback_rules(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["rule_text"] == "这是一条总结出的规则"
    assert rows[0]["matched_feedback_ids"] == ids


def test_regenerate_rejection_rules_drops_rules_with_too_few_matched_ids(db_path):
    """代码层兜底：LLM给出的matched_feedback_ids数量不足阈值，不信任LLM自报，直接丢弃。"""
    ids = _seed_feedback_rows(db_path, config.FEEDBACK_REJECTION_THRESHOLD)

    def fake_chat_completion(system_prompt, user_content):
        return json.dumps(
            [{"rule": "证据不足的规则", "matched_feedback_ids": ids[:1]}],
            ensure_ascii=False,
        )

    with patch("core.feedback_rules.chat_completion", fake_chat_completion):
        feedback_rules.regenerate_rejection_rules(db_path=db_path)

    assert get_feedback_rules(db_path=db_path) == []


def test_regenerate_rejection_rules_keeps_existing_rules_on_llm_error(db_path):
    """瞬时LLM故障不应清空已经总结好的规则。"""
    replace_feedback_rules([{"rule_text": "既有规则", "matched_feedback_ids": [1, 2, 3]}], db_path=db_path)
    _seed_feedback_rows(db_path, config.FEEDBACK_REJECTION_THRESHOLD)

    def raise_error(sp, uc):
        raise LLMCallError("网络错误")

    with patch("core.feedback_rules.chat_completion", raise_error):
        feedback_rules.regenerate_rejection_rules(db_path=db_path)

    rows = get_feedback_rules(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["rule_text"] == "既有规则"


def test_regenerate_rejection_rules_keeps_existing_rules_on_invalid_json(db_path):
    replace_feedback_rules([{"rule_text": "既有规则", "matched_feedback_ids": [1, 2, 3]}], db_path=db_path)
    _seed_feedback_rows(db_path, config.FEEDBACK_REJECTION_THRESHOLD)

    with patch("core.feedback_rules.chat_completion", lambda sp, uc: "这不是合法JSON"):
        feedback_rules.regenerate_rejection_rules(db_path=db_path)

    rows = get_feedback_rules(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["rule_text"] == "既有规则"


# ---------------------------------------------------------------------------
# core/feedback_rules.py：load_rejection_rules_text
# ---------------------------------------------------------------------------

def test_load_rejection_rules_text_empty_table_returns_empty_string(db_path):
    assert feedback_rules.load_rejection_rules_text(db_path=db_path) == ""


def test_load_rejection_rules_text_formats_rules(db_path):
    replace_feedback_rules(
        [
            {"rule_text": "规则一", "matched_feedback_ids": [1, 2, 3]},
            {"rule_text": "规则二", "matched_feedback_ids": [4, 5, 6]},
        ],
        db_path=db_path,
    )
    text = feedback_rules.load_rejection_rules_text(db_path=db_path)
    assert "规则一" in text
    assert "规则二" in text


# ---------------------------------------------------------------------------
# core/workflow/run.py：run_standard_proofread 接入 load_rejection_rules_text
# ---------------------------------------------------------------------------

def test_run_standard_proofread_loads_and_forwards_rejection_rules_text():
    fake_parsed = MagicMock(spec=ParsedDocument)
    fake_chunked = MagicMock(spec=ChunkedDocument)
    fake_proofread_result = MagicMock(spec=ProofreadResult)
    fake_classified = MagicMock(spec=ClassifiedResult)

    with patch("core.workflow.run.parse_document", return_value=fake_parsed), \
         patch("core.workflow.run.chunk_document", return_value=fake_chunked), \
         patch("core.workflow.run.load_rejection_rules_text", return_value="- 既有规则") as mock_load, \
         patch("core.workflow.run.proofread_document", return_value=fake_proofread_result) as mock_proofread, \
         patch("core.workflow.run.classify_issues", return_value=fake_classified):
        run_standard_proofread("dummy.pdf", db_path="fake.db")

    mock_load.assert_called_once_with(db_path="fake.db")
    assert mock_proofread.call_args.kwargs["rejection_rules_text"] == "- 既有规则"


def test_run_standard_proofread_forwards_empty_rejection_rules_text_by_default():
    fake_parsed = MagicMock(spec=ParsedDocument)
    fake_chunked = MagicMock(spec=ChunkedDocument)
    fake_proofread_result = MagicMock(spec=ProofreadResult)
    fake_classified = MagicMock(spec=ClassifiedResult)

    with patch("core.workflow.run.parse_document", return_value=fake_parsed), \
         patch("core.workflow.run.chunk_document", return_value=fake_chunked), \
         patch("core.workflow.run.load_rejection_rules_text", return_value=""), \
         patch("core.workflow.run.proofread_document", return_value=fake_proofread_result) as mock_proofread, \
         patch("core.workflow.run.classify_issues", return_value=fake_classified):
        run_standard_proofread("dummy.pdf")

    assert mock_proofread.call_args.kwargs["rejection_rules_text"] == ""


# ---------------------------------------------------------------------------
# app.py：拒绝按钮记录反馈+触发规则重新生成 + "反馈学习"管理页（streamlit.testing.v1.AppTest）
# ---------------------------------------------------------------------------

def _classified_result_one_confirmed_issue():
    from core.classifier import ClassifiedIssue

    issue = ClassifiedIssue(
        original_text="管理控台一组织权限一用户管理",
        issue_type="错别字与拼写",
        suggestion=_PATH_SEP_SUGGESTION,
        reason="疑似标点误用",
        block_index=0,
        page_location="第1页",
        chunk_index=0,
        located=True,
        layer=config.LAYER_CONFIRMED,
        priority=config.PRIORITY_MEDIUM,
        layer_notes=["测试用例构造"],
        llm_category="normal",
        llm_confidence="high",
        original_suggestion=_PATH_SEP_SUGGESTION,
    )
    stats = {
        "total_issues": 1, "count_confirmed": 1, "count_doubtful": 0,
        "count_quotation": 0, "count_optional": 0, "high_priority_count": 0,
    }
    return ClassifiedResult(issues=[issue], stats=stats, warnings=[])


def test_app_reject_button_records_feedback_without_regenerating_rules(db_path, tmp_path, monkeypatch):
    """拒绝按钮只写status + 调用 feedback.record_rejection 快速记录反馈，**不**在这里
    同步触发 feedback_rules.regenerate_rejection_rules——那是一次几秒的LLM调用，连续
    拒绝会次次触发拖慢页面，规则重算已改到"导出Excel成功后"和反馈页手动按钮触发。"""
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path)
    from streamlit.testing.v1 import AppTest

    fake_result = _classified_result_one_confirmed_issue()
    fake_parsed = MagicMock(spec=ParsedDocument)

    with patch("core.workflow.run_standard_proofread", return_value=(fake_result, fake_parsed)), \
         patch("core.workflow.persist_result", return_value=(1, [101])), \
         patch("core.followup.get_followup_history", return_value=[]), \
         patch("core.feedback.record_rejection") as mock_record, \
         patch("core.feedback_rules.regenerate_rejection_rules") as mock_regenerate:
        at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
        at.session_state["task_id"] = _enter_task(db_path)
        at.run()

        at.file_uploader[0].upload("test.pdf", b"dummy pdf bytes", "application/pdf").run()
        start_button = next(b for b in at.button if b.label == "开始校对")
        start_button.click().run()
        assert not at.exception

        reject_button = next(b for b in at.button if b.key == "reject_101")
        reject_button.click().run()

        assert not at.exception
        mock_record.assert_called_once()
        args = mock_record.call_args.args
        assert args[1] == 101  # issue_id
        assert args[2] == 1  # record_id
        mock_regenerate.assert_not_called()  # 拒绝不再同步跑规则总结


def test_app_standard_export_triggers_rule_regeneration(db_path, tmp_path, monkeypatch):
    """标准校对页"导出Excel"成功后应自动跑一次 regenerate_rejection_rules（把本轮拒绝
    总结成规避规则），这样"拒绝"能秒响应、规则又能自动生效。"""
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path)
    from streamlit.testing.v1 import AppTest

    fake_result = _classified_result_one_confirmed_issue()
    fake_parsed = MagicMock(spec=ParsedDocument)
    fake_export_path = tmp_path / "fake_export.xlsx"
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(("页码/位置", "原文"))
    wb.save(fake_export_path)

    with patch("core.workflow.run_standard_proofread", return_value=(fake_result, fake_parsed)), \
         patch("core.workflow.persist_result", return_value=(1, [101])), \
         patch("core.followup.get_followup_history", return_value=[]), \
         patch("core.exporter.export_issues_to_excel", return_value=fake_export_path), \
         patch("core.feedback_rules.regenerate_rejection_rules") as mock_regenerate:
        at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
        at.session_state["task_id"] = _enter_task(db_path)
        at.run()

        at.file_uploader[0].upload("test.pdf", b"dummy pdf bytes", "application/pdf").run()
        next(b for b in at.button if b.label == "开始校对").click().run()
        assert not at.exception

        export_button = next(b for b in at.button if b.label == "导出Excel")
        export_button.click().run()

        assert not at.exception
        mock_regenerate.assert_called_once()


def test_feedback_management_page_shows_placeholder_when_empty(db_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))

    at.session_state["task_id"] = _enter_task(db_path)
    at.run()

    nav = next(r for r in at.radio if r.label == "功能入口")
    nav.set_value("反馈学习").run()

    assert not at.exception
    assert any("暂无总结出的规则" in info.value for info in at.info)
    assert any("暂无反馈学习记录" in info.value for info in at.info)


def test_feedback_management_page_shows_current_rules(db_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    replace_feedback_rules(
        [{"rule_text": "编辑认为编号后跟全角句号不需要修改", "matched_feedback_ids": [1, 2, 3]}],
        db_path=db_path,
    )

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))

    at.session_state["task_id"] = _enter_task(db_path)
    at.run()

    nav = next(r for r in at.radio if r.label == "功能入口")
    nav.set_value("反馈学习").run()

    assert not at.exception
    assert any("编辑认为编号后跟全角句号不需要修改" in m.value for m in at.markdown)


def test_feedback_management_page_regenerate_button_calls_regenerate(db_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    from streamlit.testing.v1 import AppTest

    with patch("core.feedback_rules.regenerate_rejection_rules") as mock_regenerate:
        at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
        at.session_state["task_id"] = _enter_task(db_path)
        at.run()

        nav = next(r for r in at.radio if r.label == "功能入口")
        nav.set_value("反馈学习").run()

        regen_button = next(b for b in at.button if b.label == "重新生成规则")
        regen_button.click().run()

        assert not at.exception
        mock_regenerate.assert_called_once()


def test_feedback_management_page_forget_button_deletes_entry(db_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    feedback_id = add_feedback(
        issue_type="错别字与拼写", original_text="管理控台一组织权限一用户管理",
        suggestion=_PATH_SEP_SUGGESTION, reason="疑似标点误用", db_path=db_path,
    )

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))

    at.session_state["task_id"] = _enter_task(db_path)
    at.run()

    nav = next(r for r in at.radio if r.label == "功能入口")
    nav.set_value("反馈学习").run()

    forget_button = next(b for b in at.button if b.key == f"forget_feedback_{feedback_id}")
    forget_button.click().run()

    assert not at.exception
    assert get_feedback(db_path=db_path) == []
