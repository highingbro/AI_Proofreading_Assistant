"""阶段5验收测试：结果分层模块。

纯规则逻辑，全部不耗API额度：手工构造 RawIssue + 最小 ParsedDocument，逐条规则
精确断言。集成冒烟（@pytest.mark.integration，默认跳过）复用真实LLM输出走一次
完整 分块校对→分层 链路，只断言不抛异常、layer合法、stats自洽。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core.classifier import ClassifiedResult, classify_issue, classify_issues
from core.chunker import Chunk, ChunkedDocument
from core.parser import ParsedBlock, ParsedDocument
from core.proofreader import ProofreadResult, RawIssue, proofread_chunk


def _raw_issue(**overrides) -> RawIssue:
    base = dict(
        original_text="示例原文",
        issue_type="标点符号问题",
        category="normal",
        confidence="high",
        suggestion="建议修改",
        reason="示例依据",
        block_index=0,
        page_location="第1页",
        chunk_index=0,
        located=True,
    )
    base.update(overrides)
    return RawIssue(**base)


def _block(block_index=0, ocr_confidence=None, text="示例原文块内容用于测试") -> ParsedBlock:
    return ParsedBlock(
        page=1, block_index=block_index, text=text, block_type="paragraph",
        source_location="第1页", ocr_confidence=ocr_confidence,
    )


def _parsed(blocks: list[ParsedBlock]) -> ParsedDocument:
    return ParsedDocument(
        file_name="synthetic.docx", file_type="docx", total_pages=1,
        blocks=blocks, layout_mode="single", text_source="native", warnings=[],
    )


def _notes_text(issue) -> str:
    return "; ".join(issue.layer_notes)


# ---------------------------------------------------------------------------
# 规则A：引文强制保护
# ---------------------------------------------------------------------------

def test_rule_a_llm_self_reported_quotation():
    raw = _raw_issue(category="quotation", original_text="普通文本")
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_QUOTATION
    assert issue.suggestion.startswith("原文照录，不建议改动。")
    assert issue.priority == config.PRIORITY_LOW


def test_rule_a_book_title_heuristic_overrides_llm_miss():
    """核心断言：LLM没自报quotation，但original_text含书名号，系统层仍必须强制保护。"""
    raw = _raw_issue(
        category="normal", issue_type="标点符号问题",
        original_text="《红楼梦》里写道贾宝玉出场时的场景",
    )
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_QUOTATION
    assert "文本特征" in _notes_text(issue)


def test_rule_a_classical_particle_density_heuristic():
    raw = _raw_issue(
        category="normal", issue_type="上下文语义纠错",
        original_text="学而时习之，不亦说乎？有朋自远方来，不亦乐乎？人不知而不愠，不亦君子乎？",
    )
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_QUOTATION


# ---------------------------------------------------------------------------
# 规则B：事实性置信度降级
# ---------------------------------------------------------------------------

def test_rule_b_factual_low_confidence_downgrades_with_wording():
    raw = _raw_issue(
        category="factual", confidence="low", issue_type="常识与事实性错误",
        original_text="钱学森曾任麻省理工学院校长",
        suggestion="应删除'麻省理工学院校长'这一表述",
    )
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_DOUBTFUL
    assert issue.suggestion.startswith("存疑,建议人工核实：")
    assert raw.suggestion in issue.suggestion
    assert issue.original_suggestion == raw.suggestion


def test_rule_b_factual_downgrade_does_not_double_prefix_llm_own_wording():
    """真实数据发现的bug：LLM自己的medium/low建议有时已带"存疑，建议人工核实"措辞，
    不能再无脑拼接一次前缀，否则变成"存疑,建议人工核实：存疑，建议人工核实：..."。"""
    raw = _raw_issue(
        category="factual", confidence="medium", issue_type="常识与事实性错误",
        original_text="2026汉诺威工业博览会",
        suggestion="存疑，建议人工核实：是否应为“2026年汉诺威工业博览会”",
    )
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_DOUBTFUL
    assert issue.suggestion == raw.suggestion  # 原样保留，不重复拼接前缀
    assert issue.suggestion.count("存疑") == 1


def test_rule_b_factual_high_confidence_keeps_confirmed_with_note():
    raw = _raw_issue(category="factual", confidence="high", issue_type="常识与事实性错误")
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_CONFIRMED
    assert issue.suggestion == raw.suggestion  # 未被改写
    assert "事实类high置信,建议人工复核仍然适用" in issue.layer_notes


# ---------------------------------------------------------------------------
# 规则C：OCR低置信度降级
# ---------------------------------------------------------------------------

def test_rule_c_low_ocr_confidence_downgrades_typo_issue():
    raw = _raw_issue(issue_type="错别字与拼写", confidence="high", block_index=5)
    block_by_index = {5: _block(block_index=5, ocr_confidence=0.5)}
    issue = classify_issue(raw, block_by_index)
    assert issue.layer == config.LAYER_DOUBTFUL
    assert issue.suggestion.startswith("该处文字来自OCR识别且置信度较低")
    assert "低OCR置信度降级(conf=0.5)" in issue.layer_notes


def test_rule_c_high_ocr_confidence_does_not_downgrade():
    raw = _raw_issue(issue_type="错别字与拼写", confidence="high", block_index=5)
    block_by_index = {5: _block(block_index=5, ocr_confidence=0.95)}
    issue = classify_issue(raw, block_by_index)
    assert issue.layer == config.LAYER_CONFIRMED
    assert issue.suggestion == raw.suggestion


def test_rule_c_native_text_ocr_confidence_none_not_triggered():
    raw = _raw_issue(issue_type="错别字与拼写", confidence="high", block_index=5)
    block_by_index = {5: _block(block_index=5, ocr_confidence=None)}
    issue = classify_issue(raw, block_by_index)
    assert issue.layer == config.LAYER_CONFIRMED


# ---------------------------------------------------------------------------
# 规则D：未定位条目降级
# ---------------------------------------------------------------------------

def test_rule_d_unlocated_caps_confirmed_to_doubtful():
    raw = _raw_issue(confidence="high", located=False, block_index=None, page_location=None)
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_DOUBTFUL
    assert "原文未能定位回原稿" in issue.layer_notes


def test_rule_d_unlocated_quotation_stays_quotation():
    raw = _raw_issue(category="quotation", located=False, block_index=None, page_location=None)
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_QUOTATION


# ---------------------------------------------------------------------------
# 规则E：风格可选
# ---------------------------------------------------------------------------

def test_rule_e_style_category():
    raw = _raw_issue(category="style", confidence="high")
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_OPTIONAL
    assert issue.priority == config.PRIORITY_OPTIONAL


def test_rule_e_style_keyword_fallback():
    raw = _raw_issue(category="normal", suggestion="建议润色，读起来更通顺")
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_OPTIONAL


# ---------------------------------------------------------------------------
# 规则F：默认归层
# ---------------------------------------------------------------------------

def test_rule_f_normal_high_confidence():
    raw = _raw_issue(category="normal", confidence="high")
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_CONFIRMED
    assert issue.priority == config.PRIORITY_MEDIUM


def test_rule_f_normal_medium_confidence_no_rewording():
    raw = _raw_issue(category="normal", confidence="medium")
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_DOUBTFUL
    assert issue.suggestion == raw.suggestion  # 普通问题降级不加"人工核实"话术


# ---------------------------------------------------------------------------
# 规则G：知识时效性误判豁免（补丁，真实使用中发现后追加）
# ---------------------------------------------------------------------------

def test_rule_g_downgrades_recency_doubt_within_grace_window():
    year = config.LLM_KNOWLEDGE_CUTOFF_YEAR + 3
    raw = _raw_issue(
        issue_type="常识与事实性错误", category="factual", confidence="high",
        original_text=f"{year}年公司完成了新一轮融资",
        reason="这个日期超出了我的训练数据覆盖范围，无法查证是否存在",
        suggestion="建议核实该年份是否正确",
    )
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_DOUBTFUL
    assert issue.priority == config.PRIORITY_LOW
    assert "知识时效性" in issue.suggestion
    assert "知识时效性" in _notes_text(issue)


def test_rule_g_does_not_downgrade_when_year_far_beyond_grace_window():
    """年份相差太多（超过容忍宽限期），视为真的离谱，不豁免，保留原判定。"""
    year = config.LLM_KNOWLEDGE_CUTOFF_YEAR + config.KNOWLEDGE_CUTOFF_GRACE_YEARS + 20
    raw = _raw_issue(
        issue_type="常识与事实性错误", category="factual", confidence="high",
        original_text=f"{year}年公司完成了新一轮融资",
        reason="这个日期超出了我的训练数据覆盖范围，无法查证是否存在",
        suggestion="建议核实该年份是否正确",
    )
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_CONFIRMED  # 未被规则G豁免，走规则F默认归层


def test_rule_g_does_not_trigger_without_recency_wording():
    """只是普通事实性怀疑（不涉及"太新/知识范围"这类措辞），不应被规则G误伤。"""
    year = config.LLM_KNOWLEDGE_CUTOFF_YEAR + 3
    raw = _raw_issue(
        issue_type="常识与事实性错误", category="factual", confidence="low",
        original_text=f"{year}年公司完成了新一轮融资",
        reason="根据公开资料，融资金额与此处描述不符",
        suggestion="建议核实该融资金额",
    )
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_DOUBTFUL  # 走规则B的低置信度降级，不是规则G
    assert "知识时效性" not in _notes_text(issue)


def test_rule_g_does_not_trigger_for_non_factual_issue_type():
    """即使措辞命中关键词，issue_type/category都不是事实类时不应触发。"""
    year = config.LLM_KNOWLEDGE_CUTOFF_YEAR + 3
    raw = _raw_issue(
        issue_type="标点符号问题", category="normal", confidence="high",
        original_text=f"{year}年，公司,完成了新一轮融资",
        reason="这个日期超出了我的训练数据覆盖范围",
        suggestion="逗号应改为顿号",
    )
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_CONFIRMED


# ---------------------------------------------------------------------------
# 规则H：分块边界截断误判豁免（补丁，真实使用中发现后追加）
# ---------------------------------------------------------------------------

def _chunked_document(block_indices_per_chunk: list[list[int]], source=None) -> ChunkedDocument:
    chunks = [
        Chunk(
            chunk_index=i, text="", block_indices=indices, overlap_prefix_blocks=[],
            page_range=(1, 1), char_count=0,
        )
        for i, indices in enumerate(block_indices_per_chunk)
    ]
    return ChunkedDocument(source=source, chunks=chunks, chunk_size_target=0, overlap_blocks=0, warnings=[])


def test_rule_h_downgrades_issue_at_non_final_chunk_tail_block():
    """核心场景（真实bug复现）：block是所在chunk正文最后一块（非全文档最后一块），
    不以句末标点收尾，被flag原文正是该block结尾片段——判定为疑似分块截断误判。"""
    blocks = [_block(block_index=0, text="正文……分类标签、")]
    parsed = _parsed(blocks)
    chunked = _chunked_document([[0], [1]], source=parsed)  # block 0 是 chunk0(非末块)的尾block
    raw = _raw_issue(
        block_index=0, confidence="high", issue_type="标点符号问题",
        original_text="分类标签、", suggestion="删除末尾顿号，或补充完整后续内容",
    )
    result = classify_issues(ProofreadResult(issues=[raw], chunk_warnings=[]), parsed, chunked)
    issue = result.issues[0]
    assert issue.layer == config.LAYER_DOUBTFUL
    assert issue.priority == config.PRIORITY_LOW
    assert "分块" in issue.suggestion
    assert any("分块" in n for n in issue.layer_notes)


def test_rule_h_does_not_trigger_when_block_ends_with_terminal_punct():
    """block本身以句号收尾，看起来是正常收束的段落，不该被当成截断。"""
    blocks = [_block(block_index=0, text="这是完整的一句话。")]
    parsed = _parsed(blocks)
    chunked = _chunked_document([[0], [1]], source=parsed)
    raw = _raw_issue(
        block_index=0, confidence="high", issue_type="标点符号问题",
        original_text="这是完整的一句话。",
    )
    result = classify_issues(ProofreadResult(issues=[raw], chunk_warnings=[]), parsed, chunked)
    assert result.issues[0].layer == config.LAYER_CONFIRMED


def test_rule_h_does_not_trigger_for_final_chunk_of_document():
    """block虽是所在chunk的尾block，但那个chunk本身就是全文档最后一个chunk，后面没有
    更多分块内容，不存在"看不到后续"的问题，不豁免。"""
    blocks = [_block(block_index=0, text="正文……分类标签、")]
    parsed = _parsed(blocks)
    chunked = _chunked_document([[0]], source=parsed)  # 只有一个chunk，它自己就是最后一个
    raw = _raw_issue(
        block_index=0, confidence="high", issue_type="标点符号问题",
        original_text="分类标签、",
    )
    result = classify_issues(ProofreadResult(issues=[raw], chunk_warnings=[]), parsed, chunked)
    assert result.issues[0].layer == config.LAYER_CONFIRMED


def test_rule_h_does_not_trigger_when_block_is_not_chunk_tail():
    """block不是它所在chunk正文的最后一个block（后面还有同chunk内容），不该豁免。"""
    blocks = [_block(block_index=0, text="正文……分类标签、")]
    parsed = _parsed(blocks)
    chunked = _chunked_document([[0, 1], [2]], source=parsed)  # block 0 不是 chunk0 的尾block
    raw = _raw_issue(
        block_index=0, confidence="high", issue_type="标点符号问题",
        original_text="分类标签、",
    )
    result = classify_issues(ProofreadResult(issues=[raw], chunk_warnings=[]), parsed, chunked)
    assert result.issues[0].layer == config.LAYER_CONFIRMED


def test_rule_h_does_not_trigger_when_flagged_text_not_at_block_tail():
    """被flag的原文不在block结尾处（block尾还有别的没被flag的内容），说明LLM的怀疑
    不是针对"看不到后续"这件事本身，不该被规则H误伤。"""
    blocks = [_block(block_index=0, text="分类标签、还有更多没被flag的结尾内容")]
    parsed = _parsed(blocks)
    chunked = _chunked_document([[0], [1]], source=parsed)
    raw = _raw_issue(
        block_index=0, confidence="high", issue_type="标点符号问题",
        original_text="分类标签、",
    )
    result = classify_issues(ProofreadResult(issues=[raw], chunk_warnings=[]), parsed, chunked)
    assert result.issues[0].layer == config.LAYER_CONFIRMED


def test_rule_h_does_not_trigger_without_chunked_argument():
    """不传chunked（如离线调规则场景），规则H不生效，行为等同规则H新增之前。"""
    blocks = [_block(block_index=0, text="正文……分类标签、")]
    parsed = _parsed(blocks)
    raw = _raw_issue(
        block_index=0, confidence="high", issue_type="标点符号问题",
        original_text="分类标签、",
    )
    result = classify_issues(ProofreadResult(issues=[raw], chunk_warnings=[]), parsed)
    assert result.issues[0].layer == config.LAYER_CONFIRMED


# ---------------------------------------------------------------------------
# 优先级覆盖
# ---------------------------------------------------------------------------

def test_priority_override_for_political_sensitive_stays_high_even_downgraded():
    raw = _raw_issue(issue_type="政治敏感性表述", category="normal", confidence="low")
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_DOUBTFUL  # 正常按规则F降级
    assert issue.priority == config.PRIORITY_HIGH  # 但优先级仍强制为高


def test_priority_override_applies_even_on_quotation_layer():
    raw = _raw_issue(issue_type="民族与地名规范", category="quotation")
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_QUOTATION
    assert issue.priority == config.PRIORITY_HIGH


# ---------------------------------------------------------------------------
# 规则顺序（顺序敏感性 / 修饰叠加）
# ---------------------------------------------------------------------------

def test_rule_order_a_before_b_when_both_hit():
    """引文里含人名改动建议：同时命中A(书名号特征)和B(factual)，必须落引文类。"""
    raw = _raw_issue(
        category="factual", confidence="low",
        original_text="《史记》记载,司马迁曾任太史令一职",
        suggestion="'太史令'应改为'太史公'",
    )
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_QUOTATION


def test_rule_c_stacks_on_top_of_base_rule_f():
    """验证"修饰叠加"设计：normal+high先经F判定确定性错误，再被C的OCR降级修饰。"""
    raw = _raw_issue(issue_type="错别字与拼写", category="normal", confidence="high", block_index=9)
    block_by_index = {9: _block(block_index=9, ocr_confidence=0.4)}
    issue = classify_issue(raw, block_by_index)
    assert issue.layer == config.LAYER_DOUBTFUL
    notes = _notes_text(issue)
    assert "默认归层:high置信度→确定性错误" in notes  # F的判定痕迹被保留
    assert "低OCR置信度降级" in notes  # C的修饰痕迹叠加在上面


# ---------------------------------------------------------------------------
# 跨块去重
# ---------------------------------------------------------------------------

def test_dedup_same_block_same_text_keeps_more_conservative():
    blocks = [_block(block_index=0, ocr_confidence=None)]
    parsed = _parsed(blocks)
    raw1 = _raw_issue(block_index=0, confidence="high", original_text="重复问题文本")  # -> 确定性错误
    raw2 = _raw_issue(block_index=0, confidence="low", original_text="重复问题文本")   # -> 存疑待核实
    result = classify_issues(ProofreadResult(issues=[raw1, raw2], chunk_warnings=[]), parsed)

    assert len(result.issues) == 1
    assert result.issues[0].layer == config.LAYER_DOUBTFUL
    assert any("去重" in w for w in result.warnings)


def test_dedup_different_block_same_text_both_kept():
    blocks = [_block(block_index=0), _block(block_index=1)]
    parsed = _parsed(blocks)
    raw1 = _raw_issue(block_index=0, original_text="重复问题文本")
    raw2 = _raw_issue(block_index=1, original_text="重复问题文本")
    result = classify_issues(ProofreadResult(issues=[raw1, raw2], chunk_warnings=[]), parsed)

    assert len(result.issues) == 2


def test_dedup_skips_unlocated_issues():
    blocks = [_block(block_index=0)]
    parsed = _parsed(blocks)
    raw1 = _raw_issue(block_index=None, located=False, page_location=None, original_text="未定位文本")
    raw2 = _raw_issue(block_index=None, located=False, page_location=None, original_text="未定位文本")
    result = classify_issues(ProofreadResult(issues=[raw1, raw2], chunk_warnings=[]), parsed)

    assert len(result.issues) == 2  # 都不参与去重，原样保留


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def test_stats_field_names_match_records_table_and_counts_are_correct():
    blocks = [_block(block_index=i) for i in range(4)]
    parsed = _parsed(blocks)
    issues = [
        _raw_issue(block_index=0, confidence="high", original_text="确定性问题"),
        _raw_issue(block_index=1, confidence="low", original_text="存疑问题"),
        _raw_issue(block_index=2, category="quotation", original_text="引文问题"),
        _raw_issue(block_index=3, category="style", original_text="风格问题"),
        _raw_issue(block_index=0, issue_type="政治敏感性表述", confidence="high", original_text="高优先级问题"),
    ]
    result = classify_issues(ProofreadResult(issues=issues, chunk_warnings=[]), parsed)

    assert set(result.stats.keys()) == {
        "total_issues", "count_confirmed", "count_doubtful",
        "count_quotation", "count_optional", "high_priority_count",
    }
    assert result.stats["total_issues"] == len(result.issues)
    assert (
        result.stats["count_confirmed"] + result.stats["count_doubtful"]
        + result.stats["count_quotation"] + result.stats["count_optional"]
        == result.stats["total_issues"]
    )
    assert result.stats["count_quotation"] == 1
    assert result.stats["count_optional"] == 1
    assert result.stats["high_priority_count"] >= 1


def test_classify_issues_preserves_chunk_warnings():
    parsed = _parsed([_block(block_index=0)])
    result = classify_issues(
        ProofreadResult(issues=[], chunk_warnings=["第0块校对失败: 模拟"]), parsed
    )
    assert result.warnings == ["第0块校对失败: 模拟"]
    assert isinstance(result, ClassifiedResult)


# ---------------------------------------------------------------------------
# 集成冒烟（消耗真实API额度，默认跳过）
# ---------------------------------------------------------------------------

_EMBEDDED_ERROR_TEXT = (
    "本次会议由项目组统一组织，旨在推进季度工作总结与下阶段计划的制定。"
    "会议开始前，主持人宣布、活动正式启动，随后邀请了张三、李四等专家代表发言。\n"
    "发言中提到，安全帽是每位施工人员必须品，任何人未佩戴不得进入现场；"
    "同时强调，携带有效证件是入场的必须条件，请大家提前准备。\n"
    "会上还引用了《论语》中的名句：\"子曰：'学而时习之，不亦说乎？"
    "有朋自远方来，不亦乐乎？'\"，以此勉励团队保持学习热情。\n"
    "此外，主持人介绍了本次特邀嘉宾——著名科学家钱学森，并提到他曾任麻省理工学院校长一职，"
    "在学术界享有盛誉。\n"
    "说实话，这次会议整体安排还挺靠谱的，大家反馈也都比较积极。"
)


@pytest.mark.integration
def test_integration_classify_real_llm_output():
    """走真实API，验证分层结果在真实LLM输出上不抛异常、四层计数自洽。"""
    blocks = [
        ParsedBlock(
            page=1, block_index=0, text=_EMBEDDED_ERROR_TEXT, block_type="paragraph", source_location="第1段"
        )
    ]
    parsed = _parsed(blocks)
    chunk = Chunk(
        chunk_index=0, text=_EMBEDDED_ERROR_TEXT, block_indices=[0],
        overlap_prefix_blocks=[], page_range=(1, 1), char_count=len(_EMBEDDED_ERROR_TEXT),
    )

    raw_issues = proofread_chunk(chunk, parsed)
    result = classify_issues(ProofreadResult(issues=raw_issues, chunk_warnings=[]), parsed)

    for issue in result.issues:
        assert issue.layer in config.LAYERS
        print(
            f"[{issue.layer}][{issue.priority}] {issue.original_text} → {issue.suggestion} "
            f"(依据: {issue.layer_notes})"
        )

    assert (
        result.stats["count_confirmed"] + result.stats["count_doubtful"]
        + result.stats["count_quotation"] + result.stats["count_optional"]
        == result.stats["total_issues"]
    )
