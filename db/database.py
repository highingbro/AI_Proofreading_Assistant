"""SQLite 连接与建表。

定义五张表：tasks（任务表）、records（流程记录表）、issues（问题明细表）、
feedback（人工反馈原始记录表）与 feedback_rules（反馈语义总结规则表，见
core/feedback_rules.py 模块 docstring）。

层级是 任务 → 校对轮次(records) → 问题(issues)：一件持续的校对工作（如"期刊A"）
是一个 task，它下面的每一次校对/比对是一条 record。
"""

import sqlite3
from datetime import datetime

import config

_CREATE_TASKS_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT '激活',
    created_at TEXT NOT NULL,
    created_by TEXT
)
"""

# task_id 在 DDL 上可空、由应用层保证必填（db.models.create_record 的 task_id 是必传
# 参数，app.py 未选任务时根本进不到校对功能）：SQLite 的 ALTER TABLE ADD COLUMN 无法
# 给已有行的新列加 NOT NULL 约束，若只在建表语句里写 NOT NULL，会导致"新建的库"和
# "迁移过来的库"schema 不一致——两边一致比多一个 DDL 约束更有价值。
_CREATE_RECORDS_SQL = """
CREATE TABLE IF NOT EXISTS records (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks (task_id),
    author TEXT,
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

_CREATE_FEEDBACK_RULES_SQL = """
CREATE TABLE IF NOT EXISTS feedback_rules (
    rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_text TEXT NOT NULL,
    matched_feedback_ids TEXT NOT NULL,
    created_at TEXT NOT NULL
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
    """给旧库的 records 表补上 mode 列。

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


_LEGACY_LAYER_LABELS = {
    "确定性错误": "错误类",
    "存疑待核实": "存疑类",
    "风格可选": "风格类",
    # "引文类" 改名前后一致，不需要迁移
}


def _migrate_rename_layer_labels(conn: sqlite3.Connection) -> None:
    """把 issues 表里旧版 layer 字段值迁移成 config.py 现在的 LAYER_* 常量值。

    config.LAYER_CONFIRMED/LAYER_DOUBTFUL/LAYER_OPTIONAL 现在的值是"错误类"/
    "存疑类"/"风格类"，但历史 issues 行落库时写入的是改名前的旧字符串"确定性错误"/
    "存疑待核实"/"风格可选"。历史记录页按
    config.LAYERS（新值）筛选时，这些旧行的 layer 字段谁都匹配不上，页面上四个
    分层折叠框会显示成"0条"，看起来像数据丢了——实际数据完好，只是新旧标签对不上
    （真实复现：record_id=28，74条issues全部因此显示不出来，只有layer本就没改名的
    "引文类"能正常匹配）。用 UPDATE 原地把旧值改成新值，天然幂等——重复调用时
    WHERE条件只会命中还没迁移过的行，已迁移的行不会被误伤。
    """
    for old, new in _LEGACY_LAYER_LABELS.items():
        conn.execute("UPDATE issues SET layer = ? WHERE layer = ?", (new, old))
    conn.commit()


def _migrate_drop_doc_page_column(conn: sqlite3.Connection) -> None:
    """删掉旧库 issues 表里的 doc_page 列（刊物自己印的页码）。

    这一列曾经只服务于 Excel 的"文档页码"列，那一列去掉之后它就没有任何消费者了；
    位置信息由 page_location（PDF物理页码）单独回答。不留着空跑的列，免得以后有人
    照着它接新代码。已存在的取值随列一起丢弃——它回答的问题 page_location 已经答了。
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(issues)")}
    if "doc_page" in cols:
        conn.execute("ALTER TABLE issues DROP COLUMN doc_page")
        conn.commit()


def _migrate_add_task_and_author_columns(conn: sqlite3.Connection) -> None:
    """给旧库的 records 表补上 task_id（所属任务）与 author（谁跑的这轮校对）两列。

    ADD COLUMN 带 REFERENCES 子句在 SQLite 里合法的前提是新列默认值为 NULL，这里
    正好满足；task_id 的回填交给 _migrate_backfill_legacy_task。author 旧行留 NULL
    ——引入署名之前的记录本来就没有"谁做的"这个信息，不臆造。
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(records)")}
    if "task_id" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN task_id INTEGER REFERENCES tasks (task_id)")
    if "author" not in cols:
        conn.execute("ALTER TABLE records ADD COLUMN author TEXT")
    conn.commit()


def _migrate_backfill_legacy_task(conn: sqlite3.Connection) -> None:
    """把 task_id still NULL 的历史记录统一归入 config.LEGACY_TASK_NAME 这个任务。

    引入任务之后，应用层任何路径都不可能再写出 task_id 为 NULL 的 record（未选任务
    时进不到校对功能），所以"task_id IS NULL"精确等价于"该功能上线前的历史数据"，
    可以放心整批回填。空库/已回填过的库里没有这样的行，此时连任务都不会建——天然
    幂等，重复调用 init_db() 不会一次次冒出新的空任务。
    """
    pending = conn.execute("SELECT COUNT(*) AS n FROM records WHERE task_id IS NULL").fetchone()
    if not pending["n"]:
        return

    row = conn.execute(
        "SELECT task_id FROM tasks WHERE name = ?", (config.LEGACY_TASK_NAME,)
    ).fetchone()
    if row is None:
        cursor = conn.execute(
            "INSERT INTO tasks (name, description, status, created_at) VALUES (?, ?, ?, ?)",
            (
                config.LEGACY_TASK_NAME,
                "引入任务概念之前的历史校对记录，可在任务页逐条改归属到真实任务。",
                config.TASK_STATUS_ACTIVE,
                datetime.now().isoformat(),
            ),
        )
        task_id = cursor.lastrowid
    else:
        task_id = row["task_id"]

    conn.execute("UPDATE records SET task_id = ? WHERE task_id IS NULL", (task_id,))
    conn.commit()


def init_db(db_path=None) -> None:
    """首次运行自动建表（若表已存在则跳过），并对旧库做必要的列迁移。

    tasks 必须先于 records 建——records.task_id 声明了指向它的外键。
    """
    conn = get_connection(db_path)
    try:
        conn.execute(_CREATE_TASKS_SQL)
        conn.execute(_CREATE_RECORDS_SQL)
        conn.execute(_CREATE_ISSUES_SQL)
        conn.execute(_CREATE_FEEDBACK_SQL)
        conn.execute(_CREATE_FEEDBACK_RULES_SQL)
        conn.commit()
        _migrate_reject_reason_to_note(conn)
        _migrate_add_mode_column(conn)
        _migrate_rename_layer_labels(conn)
        _migrate_drop_doc_page_column(conn)
        _migrate_add_task_and_author_columns(conn)
        _migrate_backfill_legacy_task(conn)
    finally:
        conn.close()
