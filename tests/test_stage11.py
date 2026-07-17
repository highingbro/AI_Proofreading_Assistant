"""阶段11验收测试：全局术语表（core/glossary.py），解决chunk间互不可见导致的跨块一致性误判。

不耗API额度：全部 monkeypatch core.glossary.chat_completion，覆盖候选词条抽取
（频次筛选/table跳过/同频子串去重）、LLM输出解析容错（噪音丢弃/异常优雅降级）、
提示词注入（core/proofreader/prompt_builder.py 新增 glossary_text 参数）、
工作流接入（core/workflow/run.py::run_standard_proofread 构建并透传术语表）。
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import core.glossary as glossary
import core.proofreader as proofreader
from core.chunker import ChunkedDocument
from core.classifier import ClassifiedResult
from core.glossary import GlossaryEntry
from core.llm_client import LLMCallError
from core.parser import ParsedBlock, ParsedDocument
from core.proofreader import ProofreadResult
from core.workflow.run import run_standard_proofread


def _synthetic_doc(blocks: list[ParsedBlock], total_pages: int = 1) -> ParsedDocument:
    return ParsedDocument(
        file_name="synthetic.docx",
        file_type="docx",
        total_pages=total_pages,
        blocks=blocks,
        layout_mode="single",
        text_source="native",
        warnings=[],
    )


# ---------------------------------------------------------------------------
# 候选词条抽取（规则统计）
# ---------------------------------------------------------------------------

def test_extract_candidate_terms_frequency_table_skip_and_substring_dedup():
    blocks = [
        ParsedBlock(page=1, block_index=0, text="增值税发票的开具需要审核。", block_type="paragraph", source_location="第1页"),
        ParsedBlock(page=1, block_index=1, text="本季度增值税发票数量上升。", block_type="paragraph", source_location="第1页"),
        ParsedBlock(page=1, block_index=2, text="财务部负责增值税发票归档。", block_type="paragraph", source_location="第1页"),
        ParsedBlock(page=2, block_index=3, text="王五仅出现一次不该进候选表。", block_type="paragraph", source_location="第2页"),
        ParsedBlock(page=2, block_index=4, text="表格内容王五王五不该被计入候选统计。", block_type="table", source_location="第2页表格"),
    ]
    parsed = _synthetic_doc(blocks, total_pages=2)

    candidates = glossary._extract_candidate_terms(parsed)

    # 高频词条（3次）进候选表
    assert "增值税发票" in candidates
    # 子串去重：更短的子串与"增值税发票"频次相同，应被丢弃，不单独出现
    assert "增值税" not in candidates
    assert "发票" not in candidates
    # table block内容不计入统计：王五在非table block里只出现1次，达不到MIN_FREQUENCY=3
    # （若table block未被跳过，王五总频次会因table里的重复而达到3，本断言据此验证跳过生效）
    assert "王五" not in candidates


def test_extract_candidate_terms_caps_at_top_k(monkeypatch):
    monkeypatch.setattr(config, "GLOSSARY_CANDIDATE_TOP_K", 2)
    blocks = [
        ParsedBlock(page=1, block_index=0, text="甲甲甲甲乙乙乙乙丙丙丙丙", block_type="paragraph", source_location="第1页"),
    ]
    parsed = _synthetic_doc(blocks)
    candidates = glossary._extract_candidate_terms(parsed)
    assert len(candidates) <= 2


# ---------------------------------------------------------------------------
# build_glossary：LLM分类解析与优雅降级
# ---------------------------------------------------------------------------

def test_build_glossary_parses_llm_output_with_variants(monkeypatch):
    monkeypatch.setattr(glossary, "_extract_candidate_terms", lambda parsed: ["张三", "张叁", "北京文化公司"])

    def fake_chat_completion(system_prompt, user_content):
        return json.dumps(
            [
                {"canonical": "张三", "category": "人名", "variants": ["张叁"]},
                {"canonical": "北京文化公司", "category": "机构名", "variants": []},
            ],
            ensure_ascii=False,
        )

    monkeypatch.setattr(glossary, "chat_completion", fake_chat_completion)

    entries = glossary.build_glossary(MagicMock(spec=ParsedDocument))

    assert len(entries) == 2
    assert entries[0] == GlossaryEntry(canonical="张三", category="人名", variants=["张叁"])
    assert entries[1] == GlossaryEntry(canonical="北京文化公司", category="机构名", variants=[])


def test_build_glossary_drops_noise_category(monkeypatch):
    monkeypatch.setattr(glossary, "_extract_candidate_terms", lambda parsed: ["的情况下"])
    monkeypatch.setattr(
        glossary,
        "chat_completion",
        lambda sp, uc: json.dumps([{"canonical": "的情况下", "category": "噪音", "variants": []}], ensure_ascii=False),
    )

    entries = glossary.build_glossary(MagicMock(spec=ParsedDocument))

    assert entries == []


def test_build_glossary_skips_llm_call_when_no_candidates(monkeypatch):
    monkeypatch.setattr(glossary, "_extract_candidate_terms", lambda parsed: [])
    mock_chat = MagicMock()
    monkeypatch.setattr(glossary, "chat_completion", mock_chat)

    entries = glossary.build_glossary(MagicMock(spec=ParsedDocument))

    assert entries == []
    mock_chat.assert_not_called()


def test_build_glossary_returns_empty_on_llm_call_error(monkeypatch):
    monkeypatch.setattr(glossary, "_extract_candidate_terms", lambda parsed: ["张三"])

    def raise_error(sp, uc):
        raise LLMCallError("网络错误")

    monkeypatch.setattr(glossary, "chat_completion", raise_error)

    entries = glossary.build_glossary(MagicMock(spec=ParsedDocument))

    assert entries == []


def test_build_glossary_returns_empty_on_invalid_json(monkeypatch):
    monkeypatch.setattr(glossary, "_extract_candidate_terms", lambda parsed: ["张三"])
    monkeypatch.setattr(glossary, "chat_completion", lambda sp, uc: "这不是合法JSON")

    entries = glossary.build_glossary(MagicMock(spec=ParsedDocument))

    assert entries == []


# ---------------------------------------------------------------------------
# format_glossary_for_prompt
# ---------------------------------------------------------------------------

def test_format_glossary_for_prompt_empty_list_returns_empty_string():
    assert glossary.format_glossary_for_prompt([]) == ""


def test_format_glossary_for_prompt_includes_canonical_and_variants():
    entries = [GlossaryEntry(canonical="张三", category="人名", variants=["张叁"])]
    text = glossary.format_glossary_for_prompt(entries)
    assert "张三" in text
    assert "张叁" in text
    assert "人名" in text


# ---------------------------------------------------------------------------
# core/proofreader/prompt_builder.py::_build_system_prompt 新增 glossary_text 参数
# ---------------------------------------------------------------------------

def test_build_system_prompt_default_glossary_text_matches_no_arg_call():
    assert proofreader._build_system_prompt(config.PROOFREAD_MODE_DEEP) == proofreader._build_system_prompt(
        config.PROOFREAD_MODE_DEEP, ""
    )


def test_build_system_prompt_injects_glossary_text():
    prompt = proofreader._build_system_prompt(config.PROOFREAD_MODE_DEEP, "- 【人名】张三；其他写法：张叁")
    assert "张三" in prompt
    assert "张叁" in prompt


def test_build_system_prompt_shows_placeholder_when_glossary_empty():
    prompt = proofreader._build_system_prompt(config.PROOFREAD_MODE_DEEP, "")
    assert "（无）" in prompt


# ---------------------------------------------------------------------------
# core/workflow/run.py::run_standard_proofread 接入 build_glossary
# ---------------------------------------------------------------------------

def test_run_standard_proofread_builds_and_forwards_glossary_text():
    fake_parsed = MagicMock(spec=ParsedDocument)
    fake_chunked = MagicMock(spec=ChunkedDocument)
    fake_proofread_result = MagicMock(spec=ProofreadResult)
    fake_classified = MagicMock(spec=ClassifiedResult)
    fake_entries = [GlossaryEntry(canonical="张三", category="人名", variants=["张叁"])]

    with patch("core.workflow.run.parse_document", return_value=fake_parsed), \
         patch("core.workflow.run.chunk_document", return_value=fake_chunked), \
         patch("core.workflow.run.build_glossary", return_value=fake_entries) as mock_build_glossary, \
         patch("core.workflow.run.proofread_document", return_value=fake_proofread_result) as mock_proofread, \
         patch("core.workflow.run.load_learned_feedback", return_value=[]), \
         patch("core.workflow.run.classify_issues", return_value=fake_classified):
        run_standard_proofread("dummy.pdf")

    mock_build_glossary.assert_called_once_with(fake_parsed)
    glossary_text_passed = mock_proofread.call_args.kwargs["glossary_text"]
    assert "张三" in glossary_text_passed
    assert "张叁" in glossary_text_passed


def test_run_standard_proofread_passes_empty_glossary_text_when_no_entries():
    fake_parsed = MagicMock(spec=ParsedDocument)
    fake_chunked = MagicMock(spec=ChunkedDocument)
    fake_proofread_result = MagicMock(spec=ProofreadResult)
    fake_classified = MagicMock(spec=ClassifiedResult)

    with patch("core.workflow.run.parse_document", return_value=fake_parsed), \
         patch("core.workflow.run.chunk_document", return_value=fake_chunked), \
         patch("core.workflow.run.build_glossary", return_value=[]), \
         patch("core.workflow.run.proofread_document", return_value=fake_proofread_result) as mock_proofread, \
         patch("core.workflow.run.load_learned_feedback", return_value=[]), \
         patch("core.workflow.run.classify_issues", return_value=fake_classified):
        run_standard_proofread("dummy.pdf")

    assert mock_proofread.call_args.kwargs["glossary_text"] == ""
