"""阶段7验收测试：对话式追问处理（core/followup.py）。

core/followup.py 用 mock 掉 chat_completion 做精确断言，全部不联网不耗API额度。
core/workflow.py 新增的 context_snippet 计算用真实构造的小型 ParsedDocument 验证。
app.py 的追问UI用 streamlit.testing.v1.AppTest 做冒烟，同样打桩掉耗额度的入口。
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core.classifier import ClassifiedIssue, ClassifiedResult
from core.followup import answer_followup, get_followup_history
from core.parser import ParsedBlock, ParsedDocument
from core.workflow import persist_result
from db import database
from db.models import add_issue, create_record, create_task, get_issues, get_tasks, update_issue_followup


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


def _task(db_path) -> int:
    """建一个任务——records.task_id 是必填的，任何 create_record 之前都要先有任务。"""
    return create_task("测试任务", db_path=db_path)


def _make_issue(db_path, **overrides) -> tuple[int, int]:
    """建一条record+一条issue，返回 (record_id, issue_id)。"""
    record_id = create_record(task_id=_task(db_path), doc_name="测试文档.pdf", doc_version="", task_type="标准校对", db_path=db_path)
    fields = dict(
        record_id=record_id,
        page_location="第1页",
        original_text="示例原文片段",
        issue_type="标点符号问题",
        priority=config.PRIORITY_MEDIUM,
        layer=config.LAYER_CONFIRMED,
        suggestion="建议修改为……",
        context_snippet="前文……示例原文片段……后文",
        db_path=db_path,
    )
    fields.update(overrides)
    issue_id = add_issue(**fields)
    return record_id, issue_id


# ---------------------------------------------------------------------------
# core/followup.py 单元测试
# ---------------------------------------------------------------------------

def test_answer_followup_builds_prompt_with_issue_context(db_path):
    _, issue_id = _make_issue(db_path)

    with patch("core.followup.chat_completion", return_value="这是回答") as mock_chat:
        answer = answer_followup(issue_id, "为什么建议这么改？", db_path=db_path)

    assert answer == "这是回答"
    system_prompt, user_content = mock_chat.call_args.args
    assert "示例原文片段" in system_prompt
    assert "前文……示例原文片段……后文" in system_prompt
    assert "标点符号问题" in system_prompt
    assert config.LAYER_CONFIRMED in system_prompt
    assert "建议修改为……" in system_prompt
    assert "为什么建议这么改？" in user_content


def test_answer_followup_persists_and_returns_history(db_path):
    _, issue_id = _make_issue(db_path)

    with patch("core.followup.chat_completion", return_value="第一次回答"):
        answer_followup(issue_id, "问题一", db_path=db_path)

    history = get_followup_history(issue_id, db_path=db_path)
    assert len(history) == 1
    assert history[0]["question"] == "问题一"
    assert history[0]["answer"] == "第一次回答"
    assert "asked_at" in history[0]

    with patch("core.followup.chat_completion", return_value="第二次回答") as mock_chat:
        answer_followup(issue_id, "问题二", db_path=db_path)
        _, user_content = mock_chat.call_args.args
        assert "问题一" in user_content
        assert "第一次回答" in user_content
        assert "问题二" in user_content

    history = get_followup_history(issue_id, db_path=db_path)
    assert len(history) == 2
    assert [turn["question"] for turn in history] == ["问题一", "问题二"]


def test_answer_followup_only_includes_recent_history_turns(db_path):
    _, issue_id = _make_issue(db_path)
    total_turns = config.FOLLOWUP_MAX_HISTORY_TURNS + 2
    old_history = [
        {"question": f"旧问题{i}", "answer": f"旧回答{i}", "asked_at": "2020-01-01T00:00:00"}
        for i in range(total_turns)
    ]
    update_issue_followup(issue_id, json.dumps(old_history, ensure_ascii=False), db_path=db_path)

    with patch("core.followup.chat_completion", return_value="新回答") as mock_chat:
        answer_followup(issue_id, "新问题", db_path=db_path)

    _, user_content = mock_chat.call_args.args
    # 最早的两轮（被裁掉的部分）不应出现
    assert "旧问题0" not in user_content
    assert "旧问题1" not in user_content
    # 最近 FOLLOWUP_MAX_HISTORY_TURNS 轮应该出现
    for i in range(2, total_turns):
        assert f"旧问题{i}" in user_content


def test_answer_followup_without_context_snippet_still_answers(db_path):
    _, issue_id = _make_issue(db_path, context_snippet=None)

    with patch("core.followup.chat_completion", return_value="回答") as mock_chat:
        answer = answer_followup(issue_id, "追问", db_path=db_path)

    assert answer == "回答"
    system_prompt, _ = mock_chat.call_args.args
    assert "未能定位到原文上下文" in system_prompt


def test_answer_followup_nonexistent_issue_raises(db_path):
    with pytest.raises(ValueError):
        answer_followup(99999, "问题", db_path=db_path)


def test_get_followup_history_empty_for_fresh_issue(db_path):
    _, issue_id = _make_issue(db_path)
    assert get_followup_history(issue_id, db_path=db_path) == []


def test_get_followup_history_nonexistent_issue_raises(db_path):
    with pytest.raises(ValueError):
        get_followup_history(99999, db_path=db_path)


# ---------------------------------------------------------------------------
# core/workflow.py：context_snippet 计算
# ---------------------------------------------------------------------------

def _classified_issue(**overrides) -> ClassifiedIssue:
    base = dict(
        original_text="示例原文",
        issue_type="标点符号问题",
        suggestion="建议修改",
        reason="示例依据",
        block_index=2,
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


def _make_parsed_document(num_blocks: int) -> ParsedDocument:
    blocks = [
        ParsedBlock(
            page=1,
            block_index=i,
            text=f"第{i}块正文内容",
            block_type="paragraph",
            source_location=f"第{i}段",
        )
        for i in range(num_blocks)
    ]
    return ParsedDocument(file_name="测试.docx", file_type="docx", total_pages=num_blocks, blocks=blocks)


def _expected_snippet(parsed: ParsedDocument, block_index: int) -> str:
    window = config.FOLLOWUP_CONTEXT_WINDOW_BLOCKS
    lo = max(0, block_index - window)
    hi = min(len(parsed.blocks) - 1, block_index + window)
    return "\n".join(b.text for b in parsed.blocks[lo : hi + 1])


def test_persist_result_computes_context_snippet_when_parsed_given(db_path):
    parsed = _make_parsed_document(num_blocks=7)
    located_issue = _classified_issue(block_index=3, original_text="第3块正文内容")
    unlocated_issue = _classified_issue(
        block_index=None, page_location=None, located=False, original_text="未定位问题"
    )
    result = ClassifiedResult(
        issues=[located_issue, unlocated_issue],
        stats={
            "total_issues": 2,
            "count_confirmed": 2,
            "count_doubtful": 0,
            "count_quotation": 0,
            "count_optional": 0,
            "high_priority_count": 0,
        },
    )

    record_id, issue_ids = persist_result(result, task_id=_task(db_path), doc_name="测试.docx", parsed=parsed, db_path=db_path)

    issues = {row["issue_id"]: row for row in get_issues(record_id, db_path=db_path)}
    located_row = issues[issue_ids[0]]
    assert located_row["context_snippet"] == _expected_snippet(parsed, 3)

    unlocated_row = issues[issue_ids[1]]
    assert unlocated_row["context_snippet"] is None


# ---------------------------------------------------------------------------
# app.py UI冒烟（streamlit.testing.v1.AppTest）
# ---------------------------------------------------------------------------

def test_app_followup_expander_calls_answer_followup(tmp_path, monkeypatch, db_path):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    from streamlit.testing.v1 import AppTest

    fake_result = ClassifiedResult(
        issues=[
            _classified_issue(
                block_index=0, original_text="示例问题", layer=config.LAYER_CONFIRMED, priority=config.PRIORITY_MEDIUM
            )
        ],
        stats={
            "total_issues": 1,
            "count_confirmed": 1,
            "count_doubtful": 0,
            "count_quotation": 0,
            "count_optional": 0,
            "high_priority_count": 0,
        },
    )
    fake_parsed = MagicMock(spec=ParsedDocument)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path)

    with patch("core.workflow.run_standard_proofread", return_value=(fake_result, fake_parsed)), \
         patch("core.workflow.persist_result", return_value=(1, [101])), \
         patch("core.followup.get_followup_history", return_value=[]), \
         patch("core.followup.answer_followup", return_value="这是追问回答") as mock_answer:
        at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
        at.session_state["task_id"] = _enter_task(db_path)
        at.run()

        at.file_uploader[0].upload("test.pdf", b"dummy pdf bytes", "application/pdf").run()
        start_button = next(b for b in at.button if b.label == "开始校对")
        start_button.click().run()
        assert not at.exception

        followup_input = next(t for t in at.text_input if t.key == "followup_input_101")
        followup_input.input("为什么建议这么改？").run()

        submit_button = next(b for b in at.button if b.key == "followup_submit_101")
        submit_button.click().run()

        assert not at.exception
        mock_answer.assert_called_once_with(101, "为什么建议这么改？")
