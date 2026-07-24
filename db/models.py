"""数据访问函数（基础 CRUD）。

所有函数支持通过 db_path 参数指定数据库文件（便于测试使用临时数据库），
不传时使用 config.DB_PATH。
"""

import json
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
    mode: str | None = None,
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
                high_priority_count, accepted_count, rejected_count, result_path, mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                mode,
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
    note: str | None = None,
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
                layer, suggestion, status, note, context_snippet,
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
                note,
                context_snippet,
                followup_history,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def update_issue_status(issue_id: int, status: str, db_path=None) -> None:
    """更新问题的处理状态。批注（note）与状态无关，用 update_issue_note 单独更新。"""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE issues SET status = ? WHERE issue_id = ?",
            (status, issue_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_issue_note(issue_id: int, note: str, db_path=None) -> None:
    """更新某条问题的批注（与采纳/拒绝状态无关，用户可写可不写）。"""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE issues SET note = ? WHERE issue_id = ?",
            (note, issue_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_issue(issue_id: int, db_path=None) -> dict | None:
    """按 issue_id 查询单条问题，不存在返回 None。"""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM issues WHERE issue_id = ?", (issue_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def update_issue_followup(issue_id: int, followup_history: str, db_path=None) -> None:
    """更新某条问题的追问历史（JSON字符串，编解码由 core/followup.py 负责）。"""
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE issues SET followup_history = ? WHERE issue_id = ?",
            (followup_history, issue_id),
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


def add_feedback(
    issue_type: str,
    original_text: str,
    suggestion: str,
    reason: str | None = None,
    source_issue_id: int | None = None,
    source_record_id: int | None = None,
    db_path=None,
) -> int:
    """插入一条人工反馈记录（拒绝issue时调用），返回 feedback_id。"""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """
            INSERT INTO feedback (
                created_at, issue_type, original_text, suggestion, reason,
                source_issue_id, source_record_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(),
                issue_type,
                original_text,
                suggestion,
                reason,
                source_issue_id,
                source_record_id,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_feedback(issue_type: str | None = None, db_path=None) -> list[dict]:
    """按时间倒序列出人工反馈记录，可选按 issue_type 过滤。"""
    conn = get_connection(db_path)
    try:
        if issue_type is None:
            rows = conn.execute(
                "SELECT * FROM feedback ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM feedback WHERE issue_type = ? ORDER BY created_at DESC",
                (issue_type,),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def delete_feedback(feedback_id: int, db_path=None) -> None:
    """删除一条人工反馈记录（管理页"撤销"操作调用）。"""
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM feedback WHERE feedback_id = ?", (feedback_id,))
        conn.commit()
    finally:
        conn.close()


def replace_feedback_rules(rules: list[dict], db_path=None) -> None:
    """整体替换 feedback_rules 表内容（core/feedback_rules.py 重新总结规则后调用）。

    rules 每项含 "rule_text"/"matched_feedback_ids"（后者是 list[int]，这里序列化成
    JSON字符串存TEXT列）。规则集合是每次重新总结的完整产出，不是增量更新，因此在
    同一个连接内先清空旧表再逐条插入，不逐条diff。
    """
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM feedback_rules")
        conn.executemany(
            "INSERT INTO feedback_rules (rule_text, matched_feedback_ids, created_at) VALUES (?, ?, ?)",
            [
                (rule["rule_text"], json.dumps(rule["matched_feedback_ids"]), datetime.now().isoformat())
                for rule in rules
            ],
        )
        conn.commit()
    finally:
        conn.close()


def get_feedback_rules(db_path=None) -> list[dict]:
    """按 rule_id 顺序列出当前生效的反馈规则，matched_feedback_ids 还原成 list[int]。"""
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT * FROM feedback_rules ORDER BY rule_id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["matched_feedback_ids"] = json.loads(item["matched_feedback_ids"])
            result.append(item)
        return result
    finally:
        conn.close()
