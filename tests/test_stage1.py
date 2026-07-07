"""阶段1验收测试。

使用临时数据库文件，不污染 data/app.db。
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def test_tables_created_with_expected_columns(db_path):
    record_columns = _table_columns(db_path, "records")
    expected_record_columns = {
        "record_id", "created_at", "doc_name", "doc_version", "task_type",
        "total_issues", "count_confirmed", "count_doubtful", "count_quotation",
        "count_optional", "high_priority_count", "accepted_count",
        "rejected_count", "result_path",
    }
    assert expected_record_columns.issubset(record_columns)

    issue_columns = _table_columns(db_path, "issues")
    expected_issue_columns = {
        "issue_id", "record_id", "page_location", "original_text",
        "issue_type", "priority", "layer", "suggestion", "status",
        "reject_reason", "context_snippet", "followup_history",
    }
    assert expected_issue_columns.issubset(issue_columns)


def test_full_chain_create_add_update_get(db_path):
    record_id = models.create_record(
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

    models.update_issue_status(
        issue_id, status="已拒绝", reject_reason="人工判断无误", db_path=db_path
    )

    issues = models.get_issues(record_id, db_path=db_path)
    assert len(issues) == 1
    assert issues[0]["status"] == "已拒绝"
    assert issues[0]["reject_reason"] == "人工判断无误"
    assert issues[0]["original_text"] == "这是一个测试句子"


def test_get_issues_filter_by_layer(db_path):
    record_id = models.create_record(
        doc_name="测试文档.docx", doc_version="v1", task_type="标准校对", db_path=db_path
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
