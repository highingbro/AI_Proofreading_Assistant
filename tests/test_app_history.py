"""阶段10验收测试：历史记录页（app.py，复用 core/workflow.py 既有渲染逻辑）。

历史记录页不打桩数据库读写（`_render_history_detail`/`_row_to_issue_view` 本身就是
直接读库的薄封装，没有独立业务逻辑可脱离Streamlit单测），用 monkeypatch
config.DB_PATH 指向临时库隔离测试，通过 streamlit.testing.v1.AppTest 驱动真实页面。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from db import database
from db.models import add_issue, create_record, create_task, get_tasks


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


def _seed_record_with_issues(db_path):
    """构造一条历史流程记录：一条已采纳且带批注（验证批注回显），一条待处理（验证可再次采纳/拒绝）。"""
    record_id = create_record(
        task_id=_task(db_path),
        author="张三",
        doc_name="历史测试文档.pdf",
        doc_version="",
        task_type="标准校对",
        total_issues=2,
        count_confirmed=1,
        count_doubtful=1,
        count_quotation=0,
        count_optional=0,
        high_priority_count=0,
        mode=config.PROOFREAD_MODE_DEEP,
        db_path=db_path,
    )
    issue_id_1 = add_issue(
        record_id=record_id,
        page_location="第1页",
        original_text="历史原文一",
        issue_type="错别字与拼写",
        priority=config.PRIORITY_MEDIUM,
        layer=config.LAYER_CONFIRMED,
        suggestion="改为正确写法",
        status="已采纳",
        note="上次留的批注",
        db_path=db_path,
    )
    issue_id_2 = add_issue(
        record_id=record_id,
        page_location="第2页",
        original_text="历史原文二",
        issue_type="常识与事实性错误",
        priority=config.PRIORITY_MEDIUM,
        layer=config.LAYER_DOUBTFUL,
        suggestion="存疑，建议人工核实：可能有误",
        status="待处理",
        db_path=db_path,
    )
    return record_id, issue_id_1, issue_id_2


def test_history_page_shows_placeholder_when_empty(db_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))

    at.session_state["task_id"] = _enter_task(db_path)
    at.run()

    nav = next(r for r in at.radio if r.label == "功能入口")
    nav.set_value("历史记录").run()

    assert not at.exception
    assert any("当前任务下还没有校对记录" in info.value for info in at.info)


def test_history_page_renders_record_detail_with_seeded_data(db_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    record_id, issue_id_1, issue_id_2 = _seed_record_with_issues(db_path)

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))

    at.session_state["task_id"] = _enter_task(db_path)
    at.run()

    nav = next(r for r in at.radio if r.label == "功能入口")
    nav.set_value("历史记录").run()

    assert not at.exception
    # 唯一一条记录，selectbox 默认就选中它，问题卡应该直接渲染出来，不需要额外交互
    assert any("选择一条记录查看详情" == sb.label for sb in at.selectbox)

    # 已采纳的issue_id_1不再显示采纳/拒绝按钮，改显示撤销按钮；待处理的issue_id_2
    # 仍显示采纳/拒绝按钮（问题卡渲染逻辑与实时校对流程完全复用）
    assert any(b.key == f"undo_{issue_id_1}" for b in at.button)
    assert not any(b.key == f"accept_{issue_id_1}" for b in at.button)
    assert any(b.key == f"accept_{issue_id_2}" for b in at.button)
    assert any(b.key == f"reject_{issue_id_2}" for b in at.button)

    # 已采纳条目的批注应该从库里正确回显到输入框，不是空白
    note_input = next(t for t in at.text_input if t.key == f"note_{issue_id_1}")
    assert note_input.value == "上次留的批注"


def test_history_page_accept_button_writes_through_to_db(db_path, monkeypatch):
    """在历史记录页点采纳，应该和实时校对流程一样真实写库（不是只改本地展示）。"""
    monkeypatch.setattr(config, "DB_PATH", db_path)
    record_id, issue_id_1, issue_id_2 = _seed_record_with_issues(db_path)

    from streamlit.testing.v1 import AppTest
    from db.models import get_issue

    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))

    at.session_state["task_id"] = _enter_task(db_path)
    at.run()

    nav = next(r for r in at.radio if r.label == "功能入口")
    nav.set_value("历史记录").run()

    accept_button = next(b for b in at.button if b.key == f"accept_{issue_id_2}")
    accept_button.click().run()

    assert not at.exception
    assert at.session_state["issue_status"][issue_id_2]["status"] == "已采纳"
    # 真实落库断言：直接绕过UI读库确认状态确实变了，不是只有内存里的会话状态好看
    assert get_issue(issue_id_2, db_path=db_path)["status"] == "已采纳"


def test_history_page_export_button_calls_exporter(db_path, monkeypatch, tmp_path):
    from unittest.mock import patch

    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(config, "DB_PATH", db_path)
    record_id, issue_id_1, issue_id_2 = _seed_record_with_issues(db_path)

    fake_export_path = tmp_path / "fake_export.xlsx"
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(("页码/位置", "原文"))
    wb.save(fake_export_path)

    with patch("core.exporter.export_issues_to_excel", return_value=fake_export_path) as mock_export, \
         patch("core.feedback_rules.regenerate_rejection_rules") as mock_regenerate:
        at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
        at.session_state["task_id"] = _enter_task(db_path)
        at.run()

        nav = next(r for r in at.radio if r.label == "功能入口")
        nav.set_value("历史记录").run()

        export_button = next(b for b in at.button if b.key == f"history_export_{record_id}")
        export_button.click().run()

        assert not at.exception
        mock_export.assert_called_once_with(record_id)
        assert len(at.success) >= 1
        # 历史记录页的导出不触发规则重算（那不是刚审完一批新反馈的场景，见 app.py
        # _regenerate_rules_after_export 只挂在标准校对/原稿比对页的导出后）
        mock_regenerate.assert_not_called()
