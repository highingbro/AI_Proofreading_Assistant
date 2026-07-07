"""数据访问函数（阶段1实现基础 CRUD）。

所有函数支持通过 db_path 参数指定数据库文件（便于测试使用临时数据库），
不传时使用 config.DB_PATH。
"""

from datetime import datetime

from db.database import get_connection


def create_record(
    doc_name: str,
    doc_version: str,
    task_type: str,
    total_issues: int = 0,
    count_confirmed: int = 0,
    count_doubtful: int = 0,
    count_quotation: int = 0,
    count_optional: int = 0,
    high_priority_count: int = 0,
    accepted_count: int = 0,
    rejected_count: int = 0,
    result_path: str | None = None,
    db_path=None,
) -> int:
    """插入一条流程记录，返回 record_id。"""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO records (
                created_at, doc_name, doc_version, task_type, total_issues,
                count_confirmed, count_doubtful, count_quotation, count_optional,
                high_priority_count, accepted_count, rejected_count, result_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(),
                doc_name,
                doc_version,
                task_type,
                total_issues,
                count_confirmed,
                count_doubtful,
                count_quotation,
                count_optional,
                high_priority_count,
                accepted_count,
                rejected_count,
                result_path,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_record_stats(record_id: int, db_path=None, **fields) -> None:
    """更新统计字段与结果路径。

    fields 可包含 records 表中除 record_id、created_at、doc_name、
    doc_version、task_type 以外的任意字段，仅更新传入的字段。
    """
    if not fields:
        return
    allowed = {
        "total_issues",
        "count_confirmed",
        "count_doubtful",
        "count_quotation",
        "count_optional",
        "high_priority_count",
        "accepted_count",
        "rejected_count",
        "result_path",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"未知字段: {unknown}")

    set_clause = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values())
    values.append(record_id)

    conn = get_connection(db_path)
    try:
        conn.execute(
            f"UPDATE records SET {set_clause} WHERE record_id = ?", values
        )
        conn.commit()
    finally:
        conn.close()


def add_issue(
    record_id: int,
    page_location: str,
    original_text: str,
    issue_type: str,
    priority: str,
    layer: str,
    suggestion: str,
    status: str = "待处理",
    reject_reason: str | None = None,
    context_snippet: str | None = None,
    followup_history: str | None = None,
    db_path=None,
) -> int:
    """插入一条问题，返回 issue_id。"""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO issues (
                record_id, page_location, original_text, issue_type, priority,
                layer, suggestion, status, reject_reason, context_snippet,
                followup_history
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                page_location,
                original_text,
                issue_type,
                priority,
                layer,
                suggestion,
                status,
                reject_reason,
                context_snippet,
                followup_history,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_issue_status(
    issue_id: int, status: str, reject_reason: str | None = None, db_path=None
) -> None:
    """更新问题的处理状态（及拒绝理由）。"""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE issues SET status = ?, reject_reason = ? WHERE issue_id = ?",
            (status, reject_reason, issue_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_issues(record_id: int, layer: str | None = None, db_path=None) -> list[dict]:
    """按分层筛选查询某条流程记录下的问题列表。"""
    conn = get_connection(db_path)
    try:
        if layer is None:
            rows = conn.execute(
                "SELECT * FROM issues WHERE record_id = ? ORDER BY issue_id",
                (record_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM issues WHERE record_id = ? AND layer = ? ORDER BY issue_id",
                (record_id, layer),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_records(db_path=None) -> list[dict]:
    """按时间倒序列出历史记录。"""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM records ORDER BY created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
