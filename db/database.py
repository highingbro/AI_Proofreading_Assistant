"""SQLite 连接与建表（阶段1实现）。

定义三张表：records（流程记录表）、issues（问题明细表）与 feedback（阶段12新增，
人工反馈学习记录表）。
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
    result_path TEXT,
    mode TEXT
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
    note TEXT,
    context_snippet TEXT,
    followup_history TEXT,
    FOREIGN KEY (record_id) REFERENCES records (record_id)
)
"""

_CREATE_FEEDBACK_SQL = """
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    issue_type TEXT,
    original_text TEXT,
    suggestion TEXT,
    reason TEXT,
    source_issue_id INTEGER,
    source_record_id INTEGER
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


def _migrate_reject_reason_to_note(conn: sqlite3.Connection) -> None:
    """把旧库里的 reject_reason 列迁移改名为 note（"批注"不再只在拒绝时才有意义）。

    CREATE TABLE IF NOT EXISTS 对已存在的表不会做任何列变更，真实使用中的 data/app.db
    早于本次改动就已经建过 issues 表（带 reject_reason 列），必须显式迁移才能让旧库
    继续可用。用 ALTER TABLE ... RENAME COLUMN（SQLite 3.25+，随 Python 3.11 自带的
    sqlite3 早已满足）保留列里已有的历史数据，不是丢弃重建。用 PRAGMA table_info 判断
    是否需要迁移，保证重复调用 init_db() 是幂等的（不会在已迁移过的库上再次报错）。
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(issues)")}
    if "reject_reason" in cols and "note" not in cols:
        conn.execute("ALTER TABLE issues RENAME COLUMN reject_reason TO note")
        conn.commit()


def _migrate_add_mode_column(conn: sqlite3.Connection) -> None:
    """给旧库的 records 表补上 mode 列（补丁：模式选择功能新增，非原始设计）。

    CREATE TABLE IF NOT EXISTS 对已存在的表不会做任何列变更，真实使用中的
    data/app.db 早于本次改动就已经建过 records 表（没有 mode 列），必须显式迁移。
    旧记录该列取值为 NULL（迁移前的记录本来就没有"用哪种模式跑的"这个信息，
    NULL 如实表达"未知/该功能上线前的数据"，不臆造默认值）。用 PRAGMA table_info
    判断是否需要迁移，保证重复调用 init_db() 是幂等的。
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(records)")}
    if "mode" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN mode TEXT")
        conn.commit()


def init_db(db_path=None) -> None:
    """首次运行自动建表（若表已存在则跳过），并对旧库做必要的列迁移。"""
    conn = get_connection(db_path)
    try:
        conn.execute(_CREATE_RECORDS_SQL)
        conn.execute(_CREATE_ISSUES_SQL)
        conn.execute(_CREATE_FEEDBACK_SQL)
        conn.commit()
        _migrate_reject_reason_to_note(conn)
        _migrate_add_mode_column(conn)
    finally:
        conn.close()
