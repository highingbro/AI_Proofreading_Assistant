"""阶段12验收测试：人工反馈学习自动降级（db/database.py新增feedback表、db/models.py新增
CRUD、core/feedback.py、core/workflow/run.py接入、app.py拒绝按钮记录反馈+反馈学习管理页）。

不耗API额度：全部用临时数据库+monkeypatch，不调用真实LLM。core/classifier里规则J本身
的归层判定测试在 tests/test_stage5.py（分类器规则测试历来都归在那个文件）。
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core.chunker import ChunkedDocument
from core.classifier import ClassifiedResult
from core.feedback import (
    LearnedFeedback,
    cluster_feedback,
    count_similar_rejections,
    forget_feedback,
    load_learned_feedback,
    record_rejection,
)
from core.parser import ParsedDocument
from core.proofreader import ProofreadResult
from core.workflow.run import run_standard_proofread
from db import database
from db.models import add_feedback, delete_feedback, get_feedback


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_app.db"
    database.init_db(path)
    return path


_PATH_SEP_SUGGESTION = "将'一'改为'-'或'>'等规范的路径分隔符"


# ---------------------------------------------------------------------------
# db/database.py + db/models.py：feedback 表建表与CRUD往返
# ---------------------------------------------------------------------------

def _table_columns(db_path, table_name):
    conn = database.get_connection(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {row["name"] for row in rows}
    finally:
        conn.close()


def test_feedback_table_created_with_expected_columns(db_path):
    columns = _table_columns(db_path, "feedback")
    expected = {
        "feedback_id", "created_at", "issue_type", "original_text",
        "suggestion", "reason", "source_issue_id", "source_record_id",
    }
    assert expected.issubset(columns)


def test_add_get_delete_feedback_round_trip(db_path):
    feedback_id = add_feedback(
        issue_type="错别字与拼写",
        original_text="管理控台一组织权限一用户管理",
        suggestion=_PATH_SEP_SUGGESTION,
        reason="疑似标点误用",
        source_issue_id=101,
        source_record_id=1,
        db_path=db_path,
    )
    assert feedback_id > 0

    rows = get_feedback(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["issue_type"] == "错别字与拼写"
    assert rows[0]["suggestion"] == _PATH_SEP_SUGGESTION
    assert rows[0]["reason"] == "疑似标点误用"

    filtered = get_feedback(issue_type="错别字与拼写", db_path=db_path)
    assert len(filtered) == 1
    assert get_feedback(issue_type="不存在的类型", db_path=db_path) == []

    delete_feedback(feedback_id, db_path=db_path)
    assert get_feedback(db_path=db_path) == []


# ---------------------------------------------------------------------------
# core/feedback.py：record_rejection
# ---------------------------------------------------------------------------

def test_record_rejection_from_live_flow_issue_with_reason(db_path):
    """实时校对流程的issue对象（ClassifiedIssue风格）带reason属性，应完整记录。"""
    issue = types.SimpleNamespace(
        issue_type="错别字与拼写",
        original_text="管理控台一组织权限一用户管理",
        suggestion=_PATH_SEP_SUGGESTION,
        reason="疑似标点误用",
    )
    record_rejection(issue, issue_id=101, record_id=1, db_path=db_path)

    rows = get_feedback(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["reason"] == "疑似标点误用"
    assert rows[0]["source_issue_id"] == 101
    assert rows[0]["source_record_id"] == 1


def test_record_rejection_from_history_page_issue_without_reason(db_path):
    """历史记录页复用的issue对象（_row_to_issue_view包装）没有reason属性，
    应退化为空字符串，不报错，suggestion/original_text/issue_type仍完整记录。"""
    issue = types.SimpleNamespace(
        issue_type="错别字与拼写",
        original_text="管理控台一组织权限一用户管理",
        suggestion=_PATH_SEP_SUGGESTION,
    )
    record_rejection(issue, issue_id=202, record_id=2, db_path=db_path)

    rows = get_feedback(db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["reason"] == ""
    assert rows[0]["suggestion"] == _PATH_SEP_SUGGESTION


# ---------------------------------------------------------------------------
# core/feedback.py：load_learned_feedback
# ---------------------------------------------------------------------------

def test_load_learned_feedback_round_trip(db_path):
    add_feedback(
        issue_type="错别字与拼写", original_text="原文一", suggestion=_PATH_SEP_SUGGESTION,
        reason="示例", db_path=db_path,
    )
    add_feedback(
        issue_type="标点符号问题", original_text="原文二", suggestion="改为句号",
        reason=None, db_path=db_path,
    )

    learned = load_learned_feedback(db_path=db_path)

    assert len(learned) == 2
    assert all(isinstance(entry, LearnedFeedback) for entry in learned)
    types_seen = {entry.issue_type for entry in learned}
    assert types_seen == {"错别字与拼写", "标点符号问题"}


# ---------------------------------------------------------------------------
# core/feedback.py：count_similar_rejections（纯函数，规则J依赖它做判定）
# ---------------------------------------------------------------------------

def test_count_similar_rejections_exact_original_text_match():
    learned = [
        LearnedFeedback(issue_type="错别字与拼写", original_text="管理控台一组织权限一用户管理", suggestion=_PATH_SEP_SUGGESTION),
        LearnedFeedback(issue_type="错别字与拼写", original_text="管理控台一组织权限一用户管理", suggestion=_PATH_SEP_SUGGESTION),
    ]
    count = count_similar_rejections("错别字与拼写", "管理控台一组织权限一用户管理", _PATH_SEP_SUGGESTION, "", learned)
    assert count == 2


def test_count_similar_rejections_suggestion_similarity_match_different_text():
    learned = [
        LearnedFeedback(issue_type="错别字与拼写", original_text="菜单设置一权限分配", suggestion=_PATH_SEP_SUGGESTION),
        LearnedFeedback(issue_type="错别字与拼写", original_text="订单管理一退款审核", suggestion=_PATH_SEP_SUGGESTION),
    ]
    count = count_similar_rejections("错别字与拼写", "系统管理一部门配置", _PATH_SEP_SUGGESTION, "", learned)
    assert count == 2


def test_count_similar_rejections_suggestion_similarity_match_after_normalization():
    """真实场景：编号1~7的问题都用了同一套改写模板，只是具体编号/文字不同，字面相似度
    不到阈值，但去掉数字/引号内容这些可变部分后应能识别为同一类问题。"""
    learned = [
        LearnedFeedback(
            issue_type="错别字与拼写", original_text="7．参加证书考试",
            suggestion='应改为"7.参加证书考试"或"7. 参加证书考试"',
        ),
    ]
    count = count_similar_rejections(
        "错别字与拼写", "10．发布成绩", '应改为"10.发布成绩"或"10. 发布成绩"', "", learned,
    )
    assert count == 1


def test_count_similar_rejections_reason_similarity_match():
    """suggestion完全不同、但reason归一化后相似度达到阈值，同样应计入。"""
    learned = [
        LearnedFeedback(
            issue_type="错别字与拼写", original_text="可得50 分", suggestion="删除空格",
            reason='中文排版规范中，阿拉伯数字与"分"之间不应保留空格',
        ),
    ]
    count = count_similar_rejections(
        "错别字与拼写", "默认1 分", "去掉多余的空格",
        '中文排版规范中，阿拉伯数字与"元"之间不应保留空格', learned,
    )
    assert count == 1


def test_count_similar_rejections_different_issue_type_not_counted():
    learned = [
        LearnedFeedback(issue_type="标点符号问题", original_text="管理控台一组织权限一用户管理", suggestion=_PATH_SEP_SUGGESTION),
    ]
    count = count_similar_rejections("错别字与拼写", "管理控台一组织权限一用户管理", _PATH_SEP_SUGGESTION, "", learned)
    assert count == 0


def test_count_similar_rejections_low_similarity_not_counted():
    learned = [
        LearnedFeedback(issue_type="错别字与拼写", original_text="完全不相关的原文", suggestion="建议调整语序，使句子更通顺"),
    ]
    count = count_similar_rejections("错别字与拼写", "系统管理一部门配置", _PATH_SEP_SUGGESTION, "", learned)
    assert count == 0


# ---------------------------------------------------------------------------
# core/feedback.py：cluster_feedback（管理页按相似度聚类展示，不按issue_type分组）
# ---------------------------------------------------------------------------

def test_cluster_feedback_groups_similar_entries_together(db_path):
    """编号1~3三条反馈套用同一套改写模板（归一化后应聚成一簇），加一条完全无关的
    反馈（应独立成单条簇），验证不会被issue_type相同这一点错误地混进同一簇。"""
    for n in range(1, 4):
        add_feedback(
            issue_type="错别字与拼写", original_text=f"{n}．某条目",
            suggestion=f'应改为"{n}.某条目"或"{n}. 某条目"', db_path=db_path,
        )
    add_feedback(
        issue_type="错别字与拼写", original_text="完全无关的原文",
        suggestion="建议调整语序，使句子更通顺", db_path=db_path,
    )

    clusters = cluster_feedback(get_feedback(db_path=db_path))

    sizes = sorted(len(c) for c in clusters)
    assert sizes == [1, 3]


def test_cluster_feedback_exact_text_match_not_blocked_by_fuzzy_member(db_path):
    """回归用例：真实数据踩过的坑——3条原文逐字相同的反馈，中间混入一条原文不同、
    仅靠suggestion/reason模糊匹配其中2条的反馈时，3条原文相同的仍应聚成一簇，
    不能因为这条"陌生成员"跟第3条不模糊匹配，就把原文明明相同的第3条拆出去。"""
    add_feedback(issue_type="错别字与拼写", original_text="2026 年7 月13 日", suggestion="删除数字与汉字之间的多余空格，应改为2026年7月13日", db_path=db_path)
    add_feedback(issue_type="错别字与拼写", original_text="默认1 分", suggestion="建议删除数字与单位间的空格，改为默认1分。", db_path=db_path)
    add_feedback(issue_type="错别字与拼写", original_text="2026 年7 月13 日", suggestion="删除数字与汉字间的多余空格，改为2026年7月13日", db_path=db_path)
    add_feedback(issue_type="错别字与拼写", original_text="2026 年7 月13 日", suggestion="应改为2026年7月13日，删除数字与年、月之间的多余空格", db_path=db_path)

    clusters = cluster_feedback(get_feedback(db_path=db_path))

    date_cluster = next(c for c in clusters if c[0]["original_text"] == "2026 年7 月13 日")
    assert len(date_cluster) == 3


def test_cluster_feedback_different_issue_type_not_merged(db_path):
    """suggestion字面相同，但issue_type不同，不应合并进同一簇。"""
    add_feedback(issue_type="错别字与拼写", original_text="原文一", suggestion="改为规范写法", db_path=db_path)
    add_feedback(issue_type="标点符号问题", original_text="原文二", suggestion="改为规范写法", db_path=db_path)

    clusters = cluster_feedback(get_feedback(db_path=db_path))

    assert len(clusters) == 2
    assert all(len(c) == 1 for c in clusters)


# ---------------------------------------------------------------------------
# core/feedback.py：forget_feedback
# ---------------------------------------------------------------------------

def test_forget_feedback_removes_entry(db_path):
    feedback_id = add_feedback(
        issue_type="错别字与拼写", original_text="原文", suggestion=_PATH_SEP_SUGGESTION, db_path=db_path,
    )
    assert len(get_feedback(db_path=db_path)) == 1

    forget_feedback(feedback_id, db_path=db_path)

    assert get_feedback(db_path=db_path) == []


# ---------------------------------------------------------------------------
# core/workflow/run.py：run_standard_proofread 接入 load_learned_feedback
# ---------------------------------------------------------------------------

def test_run_standard_proofread_loads_and_forwards_learned_feedback():
    fake_parsed = MagicMock(spec=ParsedDocument)
    fake_chunked = MagicMock(spec=ChunkedDocument)
    fake_proofread_result = MagicMock(spec=ProofreadResult)
    fake_classified = MagicMock(spec=ClassifiedResult)
    fake_learned = [LearnedFeedback(issue_type="错别字与拼写", original_text="原文", suggestion=_PATH_SEP_SUGGESTION)]

    with patch("core.workflow.run.parse_document", return_value=fake_parsed), \
         patch("core.workflow.run.chunk_document", return_value=fake_chunked), \
         patch("core.workflow.run.build_glossary", return_value=[]), \
         patch("core.workflow.run.proofread_document", return_value=fake_proofread_result), \
         patch("core.workflow.run.load_learned_feedback", return_value=fake_learned) as mock_load, \
         patch("core.workflow.run.classify_issues", return_value=fake_classified) as mock_classify:
        run_standard_proofread("dummy.pdf", db_path="fake.db")

    mock_load.assert_called_once_with(db_path="fake.db")
    assert mock_classify.call_args.kwargs["learned_feedback"] == fake_learned


def test_run_standard_proofread_forwards_empty_learned_feedback_by_default():
    fake_parsed = MagicMock(spec=ParsedDocument)
    fake_chunked = MagicMock(spec=ChunkedDocument)
    fake_proofread_result = MagicMock(spec=ProofreadResult)
    fake_classified = MagicMock(spec=ClassifiedResult)

    with patch("core.workflow.run.parse_document", return_value=fake_parsed), \
         patch("core.workflow.run.chunk_document", return_value=fake_chunked), \
         patch("core.workflow.run.build_glossary", return_value=[]), \
         patch("core.workflow.run.proofread_document", return_value=fake_proofread_result), \
         patch("core.workflow.run.load_learned_feedback", return_value=[]), \
         patch("core.workflow.run.classify_issues", return_value=fake_classified) as mock_classify:
        run_standard_proofread("dummy.pdf")

    assert mock_classify.call_args.kwargs["learned_feedback"] == []


# ---------------------------------------------------------------------------
# app.py：拒绝按钮记录反馈 + "反馈学习"管理页（streamlit.testing.v1.AppTest）
# ---------------------------------------------------------------------------

def _classified_result_one_confirmed_issue():
    from core.classifier import ClassifiedIssue

    issue = ClassifiedIssue(
        original_text="管理控台一组织权限一用户管理",
        issue_type="错别字与拼写",
        suggestion=_PATH_SEP_SUGGESTION,
        reason="疑似标点误用",
        block_index=0,
        page_location="第1页",
        chunk_index=0,
        located=True,
        layer=config.LAYER_CONFIRMED,
        priority=config.PRIORITY_MEDIUM,
        layer_notes=["测试用例构造"],
        llm_category="normal",
        llm_confidence="high",
        original_suggestion=_PATH_SEP_SUGGESTION,
    )
    stats = {
        "total_issues": 1, "count_confirmed": 1, "count_doubtful": 0,
        "count_quotation": 0, "count_optional": 0, "high_priority_count": 0,
    }
    return ClassifiedResult(issues=[issue], stats=stats, warnings=[])


def test_app_reject_button_records_feedback(db_path, tmp_path, monkeypatch):
    """拒绝按钮除了写status，还应调用 feedback.record_rejection 记录反馈。"""
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "UPLOADS_DIR", tmp_path)
    from streamlit.testing.v1 import AppTest

    fake_result = _classified_result_one_confirmed_issue()
    fake_parsed = MagicMock(spec=ParsedDocument)

    with patch("core.workflow.run_standard_proofread", return_value=(fake_result, fake_parsed)), \
         patch("core.workflow.persist_result", return_value=(1, [101])), \
         patch("core.followup.get_followup_history", return_value=[]), \
         patch("core.feedback.record_rejection") as mock_record:
        at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
        at.run()

        at.file_uploader[0].upload("test.pdf", b"dummy pdf bytes", "application/pdf").run()
        start_button = next(b for b in at.button if b.label == "开始校对")
        start_button.click().run()
        assert not at.exception

        reject_button = next(b for b in at.button if b.key == "reject_101")
        reject_button.click().run()

        assert not at.exception
        mock_record.assert_called_once()
        args = mock_record.call_args.args
        assert args[1] == 101  # issue_id
        assert args[2] == 1  # record_id


def test_feedback_management_page_shows_placeholder_when_empty(db_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
    at.run()

    nav = next(r for r in at.radio if r.label == "功能入口")
    nav.set_value("反馈学习").run()

    assert not at.exception
    assert any("暂无反馈学习记录" in info.value for info in at.info)


def test_feedback_management_page_groups_by_issue_type_and_shows_threshold_badge(db_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    for _ in range(config.FEEDBACK_REJECTION_THRESHOLD):
        add_feedback(
            issue_type="错别字与拼写", original_text="管理控台一组织权限一用户管理",
            suggestion=_PATH_SEP_SUGGESTION, reason="疑似标点误用", db_path=db_path,
        )

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
    at.run()

    nav = next(r for r in at.radio if r.label == "功能入口")
    nav.set_value("反馈学习").run()

    assert not at.exception
    assert any("已达到自动降级阈值" in exp.label for exp in at.expander)


def test_feedback_management_page_forget_button_deletes_entry(db_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", db_path)
    feedback_id = add_feedback(
        issue_type="错别字与拼写", original_text="管理控台一组织权限一用户管理",
        suggestion=_PATH_SEP_SUGGESTION, reason="疑似标点误用", db_path=db_path,
    )

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"))
    at.run()

    nav = next(r for r in at.radio if r.label == "功能入口")
    nav.set_value("反馈学习").run()

    forget_button = next(b for b in at.button if b.key == f"forget_feedback_{feedback_id}")
    forget_button.click().run()

    assert not at.exception
    assert get_feedback(db_path=db_path) == []
