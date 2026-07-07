"""SQLite 连接与建表（阶段1实现）。

定义两张表：records（流程记录表）与 issues（问题明细表）。
"""

import sqlite3

import config

_CREATE_RECORDS_SQL = """
CREATE TABLE IF NOT EXISTS records (
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

_CREATE_ISSUES_SQL = """
CREATE TABLE IF NOT EXISTS issues (
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
    followup_history TEXT,
    FOREIGN KEY (record_id) REFERENCES records (record_id)
)
"""


def get_connection(db_path=None) -> sqlite3.Connection:
    """获取 SQLite 连接，并开启外键约束。

    db_path 为空时使用 config.DB_PATH（测试时可传入临时路径）。
    """
    path = db_path if db_path is not None else config.DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path=None) -> None:
    """首次运行自动建表（若表已存在则跳过）。"""
    conn = get_connection(db_path)
    try:
        conn.execute(_CREATE_RECORDS_SQL)
        conn.execute(_CREATE_ISSUES_SQL)
        conn.commit()
    finally:
        conn.close()
