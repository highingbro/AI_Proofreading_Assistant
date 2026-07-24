"""验收测试：简单用户名隔离（无密码鉴权，按用户名分开保存sqlite文件/上传目录）。

不耗API额度：全部用临时目录+monkeypatch，不调用真实LLM。本次改动完全收敛在
app.py 这一层（core/、db/ 均无改动——它们早就支持 db_path 透传），因此全部通过
streamlit.testing.v1.AppTest 驱动整份 app.py 脚本黑盒验证，不单独单元测试 app.py
内部的私有函数（_sanitize_username 等）——跟既有 tests/test_app_standard_flow.py、
test_app_history.py、test_feedback.py 的惯例一致，app.py 里的私有函数从未被单独
import 测试过。

"历史记录"页的记录用 db.models.create_record 直接写库预置（同 test_app_history.py
的 _seed_record_with_issues 惯例），不需要真的跑一遍解析→LLM校对流程。
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core.classifier import ClassifiedIssue, ClassifiedResult
from db import database
from db.models import create_record


def _isolate_data_dirs(tmp_path, monkeypatch):
    """把 DATA_DIR/DB_PATH/UPLOADS_DIR 都重定向到临时目录——DB_PATH 是安全网：
    切用户名之前，脚本第一次 at.run() 时用户名还是"default"，不重定向的话会
    直接打开真实生产库 data/app.db（init_db 本身是幂等的不会破坏数据，但测试
    不应该碰真实文件）。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "app.db")
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    monkeypatch.setattr(config, "UPLOADS_DIR", uploads_dir)
    return uploads_dir


def _seed_one_record(db_path, doc_name):
    database.init_db(db_path)
    return create_record(
        doc_name=doc_name,
        doc_version="",
        task_type="标准校对",
        total_issues=1,
        count_confirmed=1,
        count_doubtful=0,
        count_quotation=0,
        count_optional=0,
        high_priority_count=0,
        mode=config.PROOFREAD_MODE_DEEP,
        db_path=db_path,
    )


def _classified_result(marker: str) -> ClassifiedResult:
    issue = ClassifiedIssue(
        original_text=f"{marker}的原文",
        issue_type="错别字与拼写",
        suggestion="建议",
        reason="说明",
        block_index=None,
        page_location="第1页",
        chunk_index=0,
        located=False,
        layer=config.LAYER_CONFIRMED,
        priority=config.PRIORITY_MEDIUM,
        layer_notes=["测试用例构造"],
        llm_category="normal",
        llm_confidence="high",
        original_suggestion="建议",
    )
    stats = {
        "total_issues": 1, "count_confirmed": 1, "count_doubtful": 0,
        "count_quotation": 0, "count_optional": 0, "high_priority_count": 0,
    }
    return ClassifiedResult(issues=[issue], stats=stats, warnings=[])


def _app_path() -> str:
    return str(Path(__file__).resolve().parent.parent / "app.py")


def _switch_username(at, username: str):
    field = next(t for t in at.text_input if t.key == "username_input")
    field.set_value(username).run()
    assert not at.exception
    return at


def _go_to_history(at):
    nav = next(r for r in at.radio if r.label == "功能入口")
    nav.set_value("历史记录").run()
    assert not at.exception
    return at


# ---------------------------------------------------------------------------
# "default" 用户名：行为应与引入本功能之前完全一致
# ---------------------------------------------------------------------------

def test_default_username_keeps_legacy_db_path_and_uploads_dir(tmp_path, monkeypatch):
    _isolate_data_dirs(tmp_path, monkeypatch)
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(_app_path())
    at.run()
    assert not at.exception

    username_field = next(t for t in at.text_input if t.key == "username_input")
    assert username_field.value == "default"

    # config.DB_PATH 本身的库应该被 init_db 建出来（走的是 db_path=None 兜底逻辑）
    assert (tmp_path / "app.db").exists()
    # 不应该额外派生一个"app_default.db"——"default"不该走按用户名派生路径这条分支
    assert not (tmp_path / "app_default.db").exists()


# ---------------------------------------------------------------------------
# 核心场景：不同用户名之间"历史记录"互不可见，数据本身不丢
# ---------------------------------------------------------------------------

def test_different_usernames_see_only_their_own_history(tmp_path, monkeypatch):
    _isolate_data_dirs(tmp_path, monkeypatch)
    _seed_one_record(tmp_path / "app_alice.db", "alice的文档.pdf")

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(_app_path())
    at.run()

    _switch_username(at, "alice")
    _go_to_history(at)
    assert len(at.selectbox) == 1
    assert "alice的文档.pdf" in at.selectbox[0].value

    _switch_username(at, "bob")
    _go_to_history(at)
    assert len(at.selectbox) == 0
    assert any("暂无历史校对记录" in info.value for info in at.info)

    _switch_username(at, "alice")
    _go_to_history(at)
    assert len(at.selectbox) == 1
    assert "alice的文档.pdf" in at.selectbox[0].value

    # 两个独立sqlite文件、两个独立上传子目录都应该真实存在于磁盘上
    assert (tmp_path / "app_alice.db").exists()
    assert (tmp_path / "app_bob.db").exists()
    assert (tmp_path / "uploads" / "alice").exists()
    assert (tmp_path / "uploads" / "bob").exists()


# ---------------------------------------------------------------------------
# 切换用户名清空当前会话展示状态（相当于换了一个人在用）
# ---------------------------------------------------------------------------

def test_switching_username_clears_previous_classified_result(tmp_path, monkeypatch):
    _isolate_data_dirs(tmp_path, monkeypatch)
    fake_result = _classified_result("alice")

    from streamlit.testing.v1 import AppTest

    with patch("core.workflow.run_standard_proofread", return_value=(fake_result, None)), \
         patch("core.followup.get_followup_history", return_value=[]):
        at = AppTest.from_file(_app_path())
        at.run()

        _switch_username(at, "alice")
        at.file_uploader[0].upload("alice.pdf", b"dummy pdf bytes", "application/pdf").run()
        start_button = next(b for b in at.button if b.label == "开始校对")
        start_button.click().run()
        assert not at.exception
        assert at.session_state["classified_result"] is fake_result

        _switch_username(at, "bob")

    assert not at.exception
    # 切用户名清空session_state后立即rerun，落在默认的"标准校对"页——该页面
    # 检测到 "issue_status" 不在session_state里，会重新调用 _reset_session_state()
    # 把 classified_result 等key以初始值（None/[]/{}）填回去，所以这里应该断言
    # 值变回了None，而不是断言key完全不存在。
    assert at.session_state["classified_result"] is None
    # 重新出现上传控件，退回上传前的初始状态
    assert len(at.file_uploader) == 1


# ---------------------------------------------------------------------------
# 用户名清洗：非法路径字符不应该污染 db 文件名
# ---------------------------------------------------------------------------

def test_username_sanitization_strips_illegal_path_characters(tmp_path, monkeypatch):
    _isolate_data_dirs(tmp_path, monkeypatch)
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(_app_path())
    at.run()

    _switch_username(at, "ali/ce:bob*01")

    assert (tmp_path / "app_alicebob01.db").exists()
