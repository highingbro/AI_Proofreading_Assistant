"""任务闸门与署名的 app.py UI 测试。

app.py 是"先选任务、再选功能"的线性流程：没有当前任务时只渲染任务选择界面，
功能入口 radio 根本不会被创建。其他 UI 冒烟测试（test_app_standard_flow.py 等）
统一用预置 session_state["task_id"] 的方式跳过这道闸门，闸门本身只在这里覆盖。

用 config.DB_PATH 指向临时库隔离测试，通过 streamlit.testing.v1.AppTest 驱动真实页面。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core.classifier import ClassifiedIssue, ClassifiedResult
from core.parser import ParsedDocument
from db import database
from db.models import create_record, create_task, get_records, get_tasks, update_task_status

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_app.db"
    database.init_db(path)
    return path


def _app(monkeypatch, db_path):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(config, "DB_PATH", db_path)
    return AppTest.from_file(APP_PATH)


def _has_nav(at) -> bool:
    return any(r.label == "功能入口" for r in at.radio)


def test_gate_hides_all_features_until_a_task_is_selected(monkeypatch, db_path):
    """空库首次运行：只有任务选择界面，四个功能页一个都进不去。"""
    at = _app(monkeypatch, db_path)
    at.run()

    assert not at.exception
    assert not _has_nav(at)
    assert not at.file_uploader  # 标准校对页的上传控件不该被渲染出来
    assert any(t.label == "任务名称" for t in at.text_input)


def test_creating_a_task_enters_it_and_reveals_features(monkeypatch, db_path):
    at = _app(monkeypatch, db_path)
    at.run()

    next(t for t in at.text_input if t.label == "任务名称").set_value("期刊A").run()
    next(b for b in at.button if b.label == "创建并进入").click().run()

    assert not at.exception
    tasks = get_tasks(db_path=db_path)
    assert [t["name"] for t in tasks] == ["期刊A"]
    assert at.session_state["task_id"] == tasks[0]["task_id"]
    assert _has_nav(at)


def test_creating_a_task_without_name_shows_error_and_creates_nothing(monkeypatch, db_path):
    at = _app(monkeypatch, db_path)
    at.run()

    next(b for b in at.button if b.label == "创建并进入").click().run()

    assert not at.exception
    assert get_tasks(db_path=db_path) == []
    assert any("任务名称不能为空" in e.value for e in at.error)


def test_duplicate_task_name_first_click_only_warns_second_click_creates(monkeypatch, db_path):
    """任务名不做唯一性校验（task_id才是真正的标识），撞名不会永久拦创建。

    但第一次点击不能直接建——警告和"创建成功后rerun跳进新任务"若落在同一次脚本
    执行里，用户几乎看不到警告就已经跳页（真实反馈过的问题）。所以撞名时第一次
    点击只弹警告、不建任务；名字不变的情况下再点一次才真正创建。
    """
    create_task("期刊A", db_path=db_path)

    at = _app(monkeypatch, db_path)
    at.run()

    next(t for t in at.text_input if t.label == "任务名称").set_value("期刊A").run()
    assert any("已有同名任务" in w.value for w in at.warning)

    next(b for b in at.button if b.label == "创建并进入").click().run()

    # 第一次点击：没有创建，还停留在闸门界面，警告还在——用户这次有机会看到它。
    assert not at.exception
    assert not _has_nav(at)
    assert [t["name"] for t in get_tasks(db_path=db_path)] == ["期刊A"]
    assert any("已有同名任务" in w.value for w in at.warning)

    next(b for b in at.button if b.label == "创建并进入").click().run()

    # 第二次点击（名字没变）：真正创建并进入。
    assert not at.exception
    names = [t["name"] for t in get_tasks(db_path=db_path)]
    assert names.count("期刊A") == 2
    assert _has_nav(at)


def test_editing_name_after_duplicate_warning_creates_immediately(monkeypatch, db_path):
    """撞名点第一下之后改主意换个不重复的名字，不该被之前那次"确认标记"卡住多点一次。"""
    create_task("期刊A", db_path=db_path)

    at = _app(monkeypatch, db_path)
    at.run()

    next(t for t in at.text_input if t.label == "任务名称").set_value("期刊A").run()
    next(b for b in at.button if b.label == "创建并进入").click().run()
    assert [t["name"] for t in get_tasks(db_path=db_path)] == ["期刊A"]  # 第一次点击未创建

    next(t for t in at.text_input if t.label == "任务名称").set_value("期刊B").run()
    next(b for b in at.button if b.label == "创建并进入").click().run()

    assert not at.exception
    assert _has_nav(at)
    names = {t["name"] for t in get_tasks(db_path=db_path)}
    assert names == {"期刊A", "期刊B"}


def test_duplicate_name_check_ignores_closed_tasks(monkeypatch, db_path):
    """已关闭的任务前端本来就看不到，跟它撞名不该冒出一条"看不见的重复"提示。"""
    closed_id = create_task("期刊A", db_path=db_path)
    update_task_status(closed_id, config.TASK_STATUS_CLOSED, db_path=db_path)

    at = _app(monkeypatch, db_path)
    at.run()

    next(t for t in at.text_input if t.label == "任务名称").set_value("期刊A").run()

    assert not any("已有同名任务" in w.value for w in at.warning)


def test_new_task_records_current_author_as_creator(monkeypatch, db_path):
    """署名在选任务之前就能填（侧边栏在闸门那一屏也渲染），并写进 tasks.created_by。"""
    at = _app(monkeypatch, db_path)
    at.run()

    next(t for t in at.text_input if t.key == "new_author_input").set_value("张三").run()
    next(t for t in at.text_input if t.label == "任务名称").set_value("期刊A").run()
    next(b for b in at.button if b.label == "创建并进入").click().run()

    assert not at.exception
    assert get_tasks(db_path=db_path)[0]["created_by"] == "张三"


def test_entering_an_existing_task_from_the_list(monkeypatch, db_path):
    task_id = create_task("期刊A", db_path=db_path)

    at = _app(monkeypatch, db_path)
    at.run()

    next(b for b in at.button if b.key == f"enter_task_{task_id}").click().run()

    assert not at.exception
    assert at.session_state["task_id"] == task_id
    assert _has_nav(at)


def test_task_list_hides_resolved_tasks_unless_checkbox_ticked(monkeypatch, db_path):
    active_id = create_task("在做的期刊", db_path=db_path)
    resolved_id = create_task("做完的期刊", db_path=db_path)

    at = _app(monkeypatch, db_path)
    at.run()

    next(s for s in at.selectbox if s.key == f"task_status_{resolved_id}").set_value(
        config.TASK_STATUS_RESOLVED
    ).run()

    assert not at.exception
    assert not any(b.key == f"enter_task_{resolved_id}" for b in at.button)
    assert any(b.key == f"enter_task_{active_id}" for b in at.button)

    at.checkbox[0].set_value(True).run()
    assert any(b.key == f"enter_task_{resolved_id}" for b in at.button)


def test_closing_a_task_hides_it_from_frontend_permanently(monkeypatch, db_path):
    """已关闭在前端任何地方都不展示——跟"已解决"不同，没有复选框能把它翻出来。

    selectbox 里选"已关闭"仍然是唯一能写入这个状态的入口，但写入后该任务立刻从
    任务选择页消失，勾选"显示已解决的任务"也找不回来；想再看到只能直接改数据库。
    """
    active_id = create_task("在做的期刊", db_path=db_path)
    closed_id = create_task("关掉的期刊", db_path=db_path)

    at = _app(monkeypatch, db_path)
    at.run()

    next(s for s in at.selectbox if s.key == f"task_status_{closed_id}").set_value(
        config.TASK_STATUS_CLOSED
    ).run()

    assert not at.exception
    assert not any(b.key == f"enter_task_{closed_id}" for b in at.button)
    assert any(b.key == f"enter_task_{active_id}" for b in at.button)

    at.checkbox[0].set_value(True).run()
    assert not any(b.key == f"enter_task_{closed_id}" for b in at.button)


def test_switching_task_returns_to_gate_but_keeps_author(monkeypatch, db_path):
    task_id = create_task("期刊A", created_by="张三", db_path=db_path)

    at = _app(monkeypatch, db_path)
    at.session_state["task_id"] = task_id
    at.run()
    next(s for s in at.selectbox if s.key == "author_select").set_value("张三").run()
    assert _has_nav(at)

    at.session_state["classified_result"] = "上一个任务留下的结果"
    next(b for b in at.button if b.label == "切换任务").click().run()

    assert not at.exception
    assert not _has_nav(at)  # 回到闸门那一屏
    assert "task_id" not in at.session_state
    assert "classified_result" not in at.session_state  # 换任务=换工作上下文，展示状态清空
    assert at.session_state["author"] == "张三"  # 署名跟任务无关，不该被清掉


def test_history_page_only_lists_current_task_records(monkeypatch, db_path):
    task_a = create_task("期刊A", db_path=db_path)
    task_b = create_task("期刊B", db_path=db_path)
    create_record(task_id=task_a, doc_name="A的稿子.pdf", doc_version="", task_type="标准校对", db_path=db_path)
    create_record(task_id=task_b, doc_name="B的稿子.pdf", doc_version="", task_type="标准校对", db_path=db_path)

    at = _app(monkeypatch, db_path)
    at.session_state["task_id"] = task_a
    at.run()
    next(r for r in at.radio if r.label == "功能入口").set_value("历史记录").run()

    assert not at.exception
    record_picker = next(s for s in at.selectbox if s.label == "选择一条记录查看详情")
    assert len(record_picker.options) == 1
    assert "A的稿子.pdf" in record_picker.options[0]


def test_moving_a_record_to_another_task(monkeypatch, db_path):
    """迁移进来的历史记录都堆在"历史归档"里，靠这个入口逐条拆到真实任务。"""
    task_a = create_task("历史归档", db_path=db_path)
    task_b = create_task("期刊B", db_path=db_path)
    record_id = create_record(
        task_id=task_a, doc_name="旧稿.pdf", doc_version="", task_type="标准校对", db_path=db_path
    )

    at = _app(monkeypatch, db_path)
    at.session_state["task_id"] = task_a
    at.run()
    next(r for r in at.radio if r.label == "功能入口").set_value("历史记录").run()

    next(s for s in at.selectbox if s.key == f"move_target_{record_id}").set_value(
        f"期刊B（{config.TASK_STATUS_ACTIVE}）"
    ).run()
    next(b for b in at.button if b.key == f"move_record_{record_id}").click().run()

    assert not at.exception
    assert get_records(task_id=task_a, db_path=db_path) == []
    assert len(get_records(task_id=task_b, db_path=db_path)) == 1


def test_move_record_dropdown_excludes_closed_tasks(monkeypatch, db_path):
    """已关闭的任务前端哪里都看不到，"移动到"下拉也不能挪进去一个进不去的任务。"""
    task_a = create_task("历史归档", db_path=db_path)
    task_closed = create_task("关掉的期刊", db_path=db_path, created_by=None)
    update_task_status(task_closed, config.TASK_STATUS_CLOSED, db_path=db_path)
    record_id = create_record(
        task_id=task_a, doc_name="旧稿.pdf", doc_version="", task_type="标准校对", db_path=db_path
    )

    at = _app(monkeypatch, db_path)
    at.session_state["task_id"] = task_a
    at.run()
    next(r for r in at.radio if r.label == "功能入口").set_value("历史记录").run()

    # 除了当前所在的task_a，唯一另一个任务task_closed已关闭——"移动到"没有别的地方可去，
    # _render_move_record_to_task 在 others 为空时压根不渲染这个expander/selectbox。
    assert not any(s.key == f"move_target_{record_id}" for s in at.selectbox)


def test_proofread_persists_current_task_and_author(monkeypatch, db_path):
    """校对结果落库时带上当前任务与署名——这是"谁在哪个任务下做了这轮"的唯一来源。"""
    task_id = create_task("期刊A", db_path=db_path)
    monkeypatch.setattr(config, "UPLOADS_DIR", db_path.parent)

    result = ClassifiedResult(
        issues=[
            ClassifiedIssue(
                original_text="原文",
                issue_type="错别字与拼写",
                suggestion="建议",
                reason="示例依据",
                block_index=0,
                page_location="第1页",
                chunk_index=0,
                located=True,
                layer=config.LAYER_CONFIRMED,
                priority=config.PRIORITY_HIGH,
                layer_notes=["测试用例构造"],
                llm_category="normal",
                llm_confidence="high",
                original_suggestion="建议",
            )
        ],
        stats={
            "total_issues": 1, "count_confirmed": 1, "count_doubtful": 0,
            "count_quotation": 0, "count_optional": 0, "high_priority_count": 1,
        },
    )

    with patch(
        "core.workflow.run_standard_proofread",
        return_value=(result, MagicMock(spec=ParsedDocument)),
    ), patch("core.workflow.persist_result", return_value=(1, [101])) as mock_persist, patch(
        "core.followup.get_followup_history", return_value=[]
    ):
        at = _app(monkeypatch, db_path)
        at.session_state["task_id"] = task_id
        at.run()
        next(t for t in at.text_input if t.key == "new_author_input").set_value("李四").run()
        at.file_uploader[0].upload("test.pdf", b"dummy pdf bytes", "application/pdf").run()
        next(b for b in at.button if b.label == "开始校对").click().run()

    assert not at.exception
    assert mock_persist.call_args.kwargs["task_id"] == task_id
    assert mock_persist.call_args.kwargs["author"] == "李四"


def test_stale_task_id_falls_back_to_the_gate(monkeypatch, db_path):
    """session_state 里记着的任务已经不存在（换库等）时，退回任务选择界面而不是崩。"""
    at = _app(monkeypatch, db_path)
    at.session_state["task_id"] = 9999
    at.run()

    assert not at.exception
    assert not _has_nav(at)
    assert "task_id" not in at.session_state
