"""阶段1验收测试。

使用临时数据库文件，不污染 data/app.db。
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from db import database, models


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_app.db"
    database.init_db(path)
    return path


def _table_columns(db_path, table_name):
    conn = database.get_connection(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {row["name"] for row in rows}
    finally:
        conn.close()


def _task(db_path) -> int:
    """建一个任务——records.task_id 是必填的，任何 create_record 之前都要先有任务。"""
    return models.create_task("测试任务", db_path=db_path)


def test_tables_created_with_expected_columns(db_path):
    task_columns = _table_columns(db_path, "tasks")
    expected_task_columns = {
        "task_id", "name", "description", "status", "created_at", "created_by",
    }
    assert expected_task_columns.issubset(task_columns)

    record_columns = _table_columns(db_path, "records")
    expected_record_columns = {
        "record_id", "task_id", "author", "created_at", "doc_name", "doc_version",
        "task_type", "total_issues", "count_confirmed", "count_doubtful",
        "count_quotation", "count_optional", "high_priority_count", "accepted_count",
        "rejected_count", "result_path",
    }
    assert expected_record_columns.issubset(record_columns)

    issue_columns = _table_columns(db_path, "issues")
    expected_issue_columns = {
        "issue_id", "record_id", "page_location", "original_text",
        "issue_type", "priority", "layer", "suggestion", "status",
        "note", "context_snippet", "followup_history",
    }
    assert expected_issue_columns.issubset(issue_columns)


def test_full_chain_create_add_update_get(db_path):
    record_id = models.create_record(
        task_id=_task(db_path),
        doc_name="测试文档.docx",
        doc_version="v1",
        task_type="标准校对",
        db_path=db_path,
    )
    assert record_id > 0

    issue_id = models.add_issue(
        record_id=record_id,
        page_location="第3页",
        original_text="这是一个测试句子",
        issue_type="错别字",
        priority="高",
        layer="确定性错误",
        suggestion="应改为...",
        db_path=db_path,
    )
    assert issue_id > 0

    models.update_issue_status(issue_id, status="已拒绝", db_path=db_path)
    models.update_issue_note(issue_id, "人工判断无误", db_path=db_path)

    issues = models.get_issues(record_id, db_path=db_path)
    assert len(issues) == 1
    assert issues[0]["status"] == "已拒绝"
    assert issues[0]["note"] == "人工判断无误"
    assert issues[0]["original_text"] == "这是一个测试句子"


def test_get_issues_filter_by_layer(db_path):
    record_id = models.create_record(
        task_id=_task(db_path), doc_name="测试文档.docx", doc_version="v1",
        task_type="标准校对", db_path=db_path,
    )
    models.add_issue(
        record_id=record_id, page_location="p1", original_text="a",
        issue_type="错别字", priority="高", layer="确定性错误",
        suggestion="改为b", db_path=db_path,
    )
    models.add_issue(
        record_id=record_id, page_location="p2", original_text="c",
        issue_type="引文", priority="可选", layer="引文类",
        suggestion="原文照录,不建议改动", db_path=db_path,
    )

    confirmed = models.get_issues(record_id, layer="确定性错误", db_path=db_path)
    quotation = models.get_issues(record_id, layer="引文类", db_path=db_path)

    assert len(confirmed) == 1
    assert confirmed[0]["original_text"] == "a"
    assert len(quotation) == 1
    assert quotation[0]["original_text"] == "c"


def test_init_db_migrates_legacy_reject_reason_column_to_note(tmp_path):
    """回归测试：真实使用中 data/app.db 早于"批注"改动就已建过 issues 表（带
    reject_reason 列）。init_db() 必须能把旧库迁移到 note 列且保留历史数据，
    重复调用 init_db() 也不应报错（幂等）。"""
    legacy_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy_path))
    conn.execute(
        """
        CREATE TABLE issues (
            issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id INTEGER NOT NULL,
            page_location TEXT,
            original_text TEXT,
            issue_type TEXT,
            priority TEXT,
            layer TEXT,
            suggestion TEXT,
            status TEXT NOT NULL DEFAULT '待处理',
            reject_reason TEXT,
            context_snippet TEXT,
            followup_history TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO issues (record_id, status, reject_reason) VALUES (1, '已拒绝', '历史拒绝理由')"
    )
    conn.commit()
    conn.close()

    database.init_db(legacy_path)
    database.init_db(legacy_path)  # 幂等：迁移过的库再次调用不应报错

    columns = _table_columns(legacy_path, "issues")
    assert "note" in columns
    assert "reject_reason" not in columns

    rows = models.get_issues(1, db_path=legacy_path)
    assert len(rows) == 1
    assert rows[0]["note"] == "历史拒绝理由"


def test_init_db_migrates_legacy_records_table_adds_mode_column(tmp_path):
    """回归测试：真实使用中 data/app.db 早于"校对模式选择"功能就已建过 records 表
    （没有 mode 列）。init_db() 必须能给旧库补上该列，历史记录保持不变（mode为NULL），
    重复调用 init_db() 也不应报错（幂等）。"""
    legacy_path = tmp_path / "legacy_records.db"
    conn = sqlite3.connect(str(legacy_path))
    conn.execute(
        """
        CREATE TABLE records (
            record_id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            doc_name TEXT,
            doc_version TEXT,
            task_type TEXT,
            total_issues INTEGER,
            count_confirmed INTEGER,
            count_doubtful INTEGER,
            count_quotation INTEGER,
            count_optional INTEGER,
            high_priority_count INTEGER,
            accepted_count INTEGER,
            rejected_count INTEGER,
            result_path TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO records (created_at, doc_name, task_type) VALUES ('2026-01-01', '旧文档.docx', '标准校对')"
    )
    conn.commit()
    conn.close()

    database.init_db(legacy_path)
    database.init_db(legacy_path)  # 幂等：迁移过的库再次调用不应报错

    columns = _table_columns(legacy_path, "records")
    assert "mode" in columns

    conn = database.get_connection(legacy_path)
    try:
        row = conn.execute("SELECT doc_name, mode FROM records WHERE record_id = 1").fetchone()
    finally:
        conn.close()
    assert row["doc_name"] == "旧文档.docx"
    assert row["mode"] is None


def test_init_db_migrates_legacy_layer_labels(tmp_path):
    """回归测试：config.LAYER_CONFIRMED/LAYER_DOUBTFUL/LAYER_OPTIONAL 曾从"确定性错误"/
    "存疑待核实"/"风格可选"改名成更短的"错误类"/"存疑类"/"风格类"（引文类未变），但历史
    issues行落库时写入的是改名前的旧字符串——真实复现：历史记录页按新常量筛选时这些旧行
    全部匹配不上、显示成"0条"，看起来像数据丢了，实际数据完好只是标签对不上（record_id=28
    真实撞到过）。init_db() 必须能把旧值原地改写成新值，重复调用 init_db() 也不应把新值
    再次改错（幂等）。"""
    legacy_path = tmp_path / "legacy_layers.db"
    database.init_db(legacy_path)  # 先按当前schema建表（layer列本就是TEXT，不需要额外建表）
    conn = database.get_connection(legacy_path)
    conn.execute(
        "INSERT INTO records (created_at, doc_name, task_type) VALUES ('2026-01-01', '旧文档.docx', '标准校对')"
    )
    conn.execute(
        "INSERT INTO issues (record_id, layer, original_text) VALUES (1, '确定性错误', 'a')"
    )
    conn.execute(
        "INSERT INTO issues (record_id, layer, original_text) VALUES (1, '存疑待核实', 'b')"
    )
    conn.execute(
        "INSERT INTO issues (record_id, layer, original_text) VALUES (1, '风格可选', 'c')"
    )
    conn.execute(
        "INSERT INTO issues (record_id, layer, original_text) VALUES (1, '引文类', 'd')"
    )
    conn.commit()
    conn.close()

    database.init_db(legacy_path)
    database.init_db(legacy_path)  # 幂等：新值不应被再次误改

    rows = {r["original_text"]: r["layer"] for r in models.get_issues(1, db_path=legacy_path)}
    assert rows == {
        "a": config.LAYER_CONFIRMED,
        "b": config.LAYER_DOUBTFUL,
        "c": config.LAYER_OPTIONAL,
        "d": config.LAYER_QUOTATION,
    }


def test_foreign_key_constraint_enforced(db_path):
    with pytest.raises(sqlite3.IntegrityError):
        models.add_issue(
            record_id=9999,
            page_location="p1",
            original_text="a",
            issue_type="错别字",
            priority="高",
            layer="确定性错误",
            suggestion="改为b",
            db_path=db_path,
        )


def test_init_db_migrates_legacy_records_into_archive_task(tmp_path):
    """回归测试：真实使用中 data/app.db 里已有几十条早于"任务"概念的记录（没有
    task_id/author 列）。init_db() 必须补上两列、把这些记录整批归入"历史归档"任务，
    重复调用不能一次次冒出新的空任务（幂等）。"""
    legacy_path = tmp_path / "legacy_tasks.db"
    conn = sqlite3.connect(str(legacy_path))
    conn.execute(
        """
        CREATE TABLE records (
            record_id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            doc_name TEXT,
            doc_version TEXT,
            task_type TEXT,
            total_issues INTEGER,
            count_confirmed INTEGER,
            count_doubtful INTEGER,
            count_quotation INTEGER,
            count_optional INTEGER,
            high_priority_count INTEGER,
            accepted_count INTEGER,
            rejected_count INTEGER,
            result_path TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO records (created_at, doc_name, task_type)"
        " VALUES ('2026-01-01', '旧文档.docx', '标准校对')"
    )
    conn.execute(
        "INSERT INTO records (created_at, doc_name, task_type)"
        " VALUES ('2026-01-02', '旧文档2.docx', '标准校对')"
    )
    conn.commit()
    conn.close()

    database.init_db(legacy_path)
    database.init_db(legacy_path)  # 幂等：不应再建一个同名任务、也不应报错

    columns = _table_columns(legacy_path, "records")
    assert {"task_id", "author"}.issubset(columns)

    tasks = models.get_tasks(db_path=legacy_path)
    assert [t["name"] for t in tasks] == [config.LEGACY_TASK_NAME]
    assert tasks[0]["status"] == config.TASK_STATUS_ACTIVE

    archive_id = tasks[0]["task_id"]
    records = models.get_records(db_path=legacy_path)
    assert len(records) == 2
    assert {r["task_id"] for r in records} == {archive_id}
    assert {r["author"] for r in records} == {None}


def test_init_db_on_fresh_db_creates_no_archive_task(db_path):
    """空库没有任何待回填的历史记录，就不该冒出一个"历史归档"空任务。"""
    assert models.get_tasks(db_path=db_path) == []


def test_task_status_update_and_record_scoping(db_path):
    """任务的状态流转、按任务筛选记录、改归属任务三件事的最小闭环。"""
    task_a = models.create_task("期刊A", created_by="张三", db_path=db_path)
    task_b = models.create_task("期刊B", db_path=db_path)

    record_id = models.create_record(
        task_id=task_a, doc_name="第1期.pdf", doc_version="", task_type="标准校对",
        author="李四", db_path=db_path,
    )

    assert [r["record_id"] for r in models.get_records(task_id=task_a, db_path=db_path)] == [record_id]
    assert models.get_records(task_id=task_b, db_path=db_path) == []
    assert models.count_records_by_task(db_path=db_path) == {task_a: 1}
    # 署名候选来自 records.author ∪ tasks.created_by，不单建人员表
    assert models.get_authors(db_path=db_path) == ["张三", "李四"]

    models.update_record_task(record_id, task_b, db_path=db_path)
    assert models.get_records(task_id=task_a, db_path=db_path) == []
    assert len(models.get_records(task_id=task_b, db_path=db_path)) == 1

    models.update_task_status(task_a, config.TASK_STATUS_CLOSED, db_path=db_path)
    assert models.get_task(task_a, db_path=db_path)["status"] == config.TASK_STATUS_CLOSED
    active = models.get_tasks(status=config.TASK_STATUS_ACTIVE, db_path=db_path)
    assert [t["task_id"] for t in active] == [task_b]
    assert models.get_task(9999, db_path=db_path) is None
