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
    assert "事实类高置信,建议人工复核仍然适用" in issue.layer_notes


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
# 规则I：PDF换行符被LLM转写成空格的解析伪影豁免（补丁，真实使用中发现后追加）
# ---------------------------------------------------------------------------

def test_rule_i_downgrades_linewrap_space_artifact():
    """真实案例：block里"教务管理\\n教师"的换行符被LLM转写成空格"教务管理教 师"上报，
    空格插入位置和真实\\n位置相差一个字符，仍应命中（整体去除后子串匹配，不要求精确对位）。
    """
    block = _block(block_index=0, text="教务管理\n教师无组织权限。")
    raw = _raw_issue(
        original_text="教务管理教 师", issue_type="错别字与拼写", confidence="high",
        suggestion="应删除空格，改为“教务管理教师”", block_index=0,
    )
    issue = classify_issue(raw, {0: block})
    assert issue.layer == config.LAYER_DOUBTFUL
    assert issue.priority == config.PRIORITY_LOW
    assert any("换行" in n for n in issue.layer_notes)


def test_rule_i_does_not_trigger_without_matching_block():
    raw = _raw_issue(
        original_text="教务管理教 师", issue_type="错别字与拼写", confidence="high", block_index=0,
    )
    issue = classify_issue(raw, {})  # block_by_index里找不到block_index=0，block为None
    assert issue.layer == config.LAYER_CONFIRMED


def test_rule_i_does_not_trigger_when_space_is_real():
    """空格在block.text里原样存在（不是靠换行符拼出来的），说明是原文真实携带的，不豁免。"""
    block = _block(block_index=0, text="选择时间段内 学习课程数量")
    raw = _raw_issue(
        original_text="时间段内 学习课程", issue_type="错别字与拼写", confidence="high", block_index=0,
    )
    issue = classify_issue(raw, {0: block})
    assert issue.layer == config.LAYER_CONFIRMED


def test_rule_i_does_not_trigger_when_no_match_after_stripping():
    """去除空格/换行符后也在block里找不到对应内容，说明不是这类伪影，保留原判定。"""
    block = _block(block_index=0, text="这是完全不相关的一段文字，没有任何关联。")
    raw = _raw_issue(
        original_text="教务管理教 师", issue_type="错别字与拼写", confidence="high", block_index=0,
    )
    issue = classify_issue(raw, {0: block})
    assert issue.layer == config.LAYER_CONFIRMED


def test_rule_i_does_not_trigger_when_layer_not_confirmed():
    """已经不是"确定性错误"的层级（比如被规则A判成引文类）不需要再降，规则I不生效。"""
    block = _block(block_index=0, text="教务管理\n教师无组织权限。")
    raw = _raw_issue(
        original_text="教务管理教 师", issue_type="错别字与拼写", category="quotation",
        confidence="high", block_index=0,
    )
    issue = classify_issue(raw, {0: block})
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
# 精简模式语法结构降级（补丁：精简/深度模式功能新增后追加，非阶段5原始设计）
# ---------------------------------------------------------------------------

def test_deep_mode_grammar_issue_unaffected_high_confidence():
    """深度模式（默认，不传mode）下语法结构问题不受影响，回归保护：高置信度仍是确定性错误。"""
    raw = _raw_issue(issue_type="语法结构问题", category="normal", confidence="high")
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_CONFIRMED


def test_deep_mode_grammar_issue_unaffected_low_confidence():
    raw = _raw_issue(issue_type="语法结构问题", category="normal", confidence="low")
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_DOUBTFUL


def test_simplified_mode_grammar_issue_downgraded_to_style_regardless_of_confidence():
    for confidence in ("high", "medium", "low"):
        raw = _raw_issue(issue_type="语法结构问题", category="normal", confidence=confidence)
        issue = classify_issue(raw, {}, mode=config.PROOFREAD_MODE_SIMPLIFIED)
        assert issue.layer == config.LAYER_OPTIONAL
        assert issue.priority == config.PRIORITY_OPTIONAL


def test_simplified_mode_quotation_protection_still_wins_over_grammar_downgrade():
    """即使issue_type是语法结构问题，精简模式下quotation特征命中仍优先生效，保护不被弱化。"""
    raw = _raw_issue(issue_type="语法结构问题", category="quotation", confidence="high")
    issue = classify_issue(raw, {}, mode=config.PROOFREAD_MODE_SIMPLIFIED)
    assert issue.layer == config.LAYER_QUOTATION


def test_simplified_mode_factual_protection_still_wins_over_grammar_downgrade():
    """即使issue_type是语法结构问题，精简模式下factual特征命中仍优先生效，保护不被弱化。"""
    raw = _raw_issue(issue_type="语法结构问题", category="factual", confidence="high")
    issue = classify_issue(raw, {}, mode=config.PROOFREAD_MODE_SIMPLIFIED)
    assert issue.layer == config.LAYER_CONFIRMED


def test_classify_issues_forwards_mode_to_classify_issue():
    raw = _raw_issue(issue_type="语法结构问题", category="normal", confidence="high", block_index=0)
    parsed = _parsed([_block(0)])
    result = classify_issues(
        ProofreadResult(issues=[raw], chunk_warnings=[]), parsed, mode=config.PROOFREAD_MODE_SIMPLIFIED
    )
    assert result.issues[0].layer == config.LAYER_OPTIONAL


# ---------------------------------------------------------------------------
# 精简模式"汉字冒充标点"降级（补丁：与语法结构降级同批真实反馈，成因不同）
# ---------------------------------------------------------------------------

def test_deep_mode_typo_as_punctuation_unaffected():
    """深度模式下不受影响，回归保护：即使建议提到"破折号"，仍按原confidence判定。"""
    raw = _raw_issue(
        issue_type="错别字与拼写", category="normal", confidence="high",
        original_text="管理控台一角色权限", suggestion="将汉字“一”改为破折号“——”或短横线“-”。",
    )
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_CONFIRMED


def test_simplified_mode_typo_as_punctuation_downgraded_to_style():
    raw = _raw_issue(
        issue_type="错别字与拼写", category="normal", confidence="high",
        original_text="管理控台一角色权限", suggestion="将汉字“一”改为破折号“——”或短横线“-”。",
    )
    issue = classify_issue(raw, {}, mode=config.PROOFREAD_MODE_SIMPLIFIED)
    assert issue.layer == config.LAYER_OPTIONAL
    assert issue.priority == config.PRIORITY_OPTIONAL


def test_simplified_mode_genuine_typo_not_downgraded():
    """建议里没有命中连接类标点关键词的真正错别字（如"的/地/得"），精简模式下不受这条规则影响。"""
    raw = _raw_issue(
        issue_type="错别字与拼写", category="normal", confidence="high",
        original_text="他吃的很开心", suggestion="将“的”改为“得”",
    )
    issue = classify_issue(raw, {}, mode=config.PROOFREAD_MODE_SIMPLIFIED)
    assert issue.layer == config.LAYER_CONFIRMED


def test_simplified_mode_quotation_protection_still_wins_over_typo_punctuation_downgrade():
    raw = _raw_issue(
        issue_type="错别字与拼写", category="quotation", confidence="high",
        suggestion="将汉字“一”改为破折号“——”",
    )
    issue = classify_issue(raw, {}, mode=config.PROOFREAD_MODE_SIMPLIFIED)
    assert issue.layer == config.LAYER_QUOTATION


def test_simplified_mode_factual_protection_still_wins_over_typo_punctuation_downgrade():
    raw = _raw_issue(
        issue_type="错别字与拼写", category="factual", confidence="high",
        suggestion="将汉字“一”改为破折号“——”",
    )
    issue = classify_issue(raw, {}, mode=config.PROOFREAD_MODE_SIMPLIFIED)
    assert issue.layer == config.LAYER_CONFIRMED


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
    assert "默认归层:高置信度→确定性错误" in notes  # F的判定痕迹被保留
    assert "低OCR置信度降级" in notes  # C的修饰痕迹叠加在上面


# ---------------------------------------------------------------------------
# 视觉无实质改动的建议过滤（真实使用中发现后追加，见 core/classifier/CLAUDE.md）
# ---------------------------------------------------------------------------

def test_no_op_filter_drops_literally_identical_suggestion():
    """真实案例：建议"应改为『X』"，X和原文字面完全一样，属于零改动的假问题，直接丢弃。"""
    parsed = _parsed([_block(block_index=0)])
    raw = _raw_issue(
        block_index=0, original_text="截至目前", suggestion='应改为"截至目前"',
    )
    result = classify_issues(ProofreadResult(issues=[raw], chunk_warnings=[]), parsed)

    assert result.issues == []
    assert any("丢弃1条视觉无实质改动的建议" in w for w in result.warnings)


def test_no_op_filter_drops_unicode_compatibility_variant_suggestion():
    """真实案例：原文用了PDF解析产生的康熙部首变体字符（如"⽉"⽽非标准汉字"月"），
    肉眼看不出区别，但LLM当成"错别字"报了出来——用NFKC规范化后两者相同，应丢弃。"""
    parsed = _parsed([_block(block_index=0)])
    kangxi_yue = "⽉"  # 康熙部首变体，NFKC规范化后等于标准汉字"月"(U+6708)
    raw = _raw_issue(
        block_index=0,
        original_text=f"{kangxi_yue}底前完成",
        suggestion=f'应改为"月底前完成"',
    )
    result = classify_issues(ProofreadResult(issues=[raw], chunk_warnings=[]), parsed)

    assert result.issues == []


def test_no_op_filter_does_not_trigger_for_genuine_rewrite():
    """真实的改写建议（原文和建议内容不同）不应被误伤。"""
    parsed = _parsed([_block(block_index=0)])
    raw = _raw_issue(block_index=0, original_text="管理控台", suggestion='应改为"管理控制台"')
    result = classify_issues(ProofreadResult(issues=[raw], chunk_warnings=[]), parsed)

    assert len(result.issues) == 1


def test_no_op_filter_does_not_trigger_for_doubtful_suggestion_quoting_original():
    """真实案例：'存疑，建议人工核实：...'这类suggestion按提示词固定格式，惯例上会在
    句子里重新引用原文做说明（不是"应改为"这个确定性改写措辞），不应被误判成零改动。"""
    parsed = _parsed([_block(block_index=0)])
    raw = _raw_issue(
        block_index=0,
        confidence="medium",
        original_text="茅恒",
        suggestion='存疑，建议人工核实："茅恒"是否为"茅台"之误，或确有此客户名称。',
    )
    result = classify_issues(ProofreadResult(issues=[raw], chunk_warnings=[]), parsed)

    assert len(result.issues) == 1


def test_no_op_filter_drops_radical_lookalike_without_nfkc_decomposition():
    """真实案例（data/app.db 35号记录）："⻔"（CJK部首补充区，无NFKC兼容分解）代替
    标准汉字"门"混入正文，NFKC规范化本身处理不了，必须走 _RADICAL_LOOKALIKE_OVERRIDES
    人工映射表才能识别为零改动。"""
    parsed = _parsed([_block(block_index=0)])
    raw = _raw_issue(block_index=0, original_text="厦⻔", suggestion='应改为"厦门"')
    result = classify_issues(ProofreadResult(issues=[raw], chunk_warnings=[]), parsed)

    assert result.issues == []


def test_no_op_filter_drops_fragment_style_suggestion_within_longer_original_text():
    """真实案例："「⺟」应改为「母」"这种只描述original_text里一个字该换的措辞，
    original_text本身是"归⺟扣⾮净利润"整个词组——整段比较（旧逻辑）会因为长度不
    一致判定"不是零改动"而漏判，必须单独识别"旧片段→新片段"这一对再比较。"""
    parsed = _parsed([_block(block_index=0)])
    raw = _raw_issue(block_index=0, original_text="归⺟扣⾮净利润", suggestion='"⺟"应改为"母"。')
    result = classify_issues(ProofreadResult(issues=[raw], chunk_warnings=[]), parsed)

    assert result.issues == []


def test_no_op_filter_drops_control_char_garbage():
    """真实案例：original_text里混入了PDF字体解析产生的SOH等控制字符（如私有区符号
    被误解析成控制码），这是解析垃圾，没有核实价值，无论suggestion说什么都直接丢弃。"""
    parsed = _parsed([_block(block_index=0)])
    raw = _raw_issue(
        block_index=0,
        original_text="总第\x01\x01\x01期\x01",
        suggestion='应补全为期号信息，如"总第79期"',
    )
    result = classify_issues(ProofreadResult(issues=[raw], chunk_warnings=[]), parsed)

    assert result.issues == []
    assert any("丢弃1条解析产生乱码字符的问题" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 规则J：版式错乱措辞 / 空格差异兜底降级（真实使用中发现后追加，见 core/classifier/CLAUDE.md）
# ---------------------------------------------------------------------------

def test_rule_j_downgrades_layout_confusion_wording():
    """真实案例：LLM自己在suggestion里承认"此处排版错乱/跨行错位"，说明这不是语言
    本身的确定性错误，而是LLM也没看懂被解析打乱的版面——必须降级，不能留在错误类。"""
    raw = _raw_issue(
        original_text="全球市场份额从2023年的51%升⾄2024年，全球机器⼈产业经历了",
        suggestion="此处文字排版错乱，'升至'后应为'54%'，但被另一段话插入，建议核实原文并重新排版",
    )
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_DOUBTFUL
    assert "版式错乱" in _notes_text(issue)


def test_rule_j_downgrades_pure_whitespace_diff_even_with_radical_lookalike_mixed_in():
    """真实案例："2 0 2 5年9⽉1 1⽇"→"2025年9月11日"：既有部首替代字（⽉），又有
    纯空格差异（数字间被拆开）——discard过滤器判定"不是零改动"（因为空格差异是真实
    的，不该被丢弃，用户要求空格问题只降级不删除），必须走空格差异兜底降级，且比较
    时要套一遍部首替代修正，否则会因为⽉≠月而漏判。"""
    raw = _raw_issue(
        original_text="2 0 2 5年9⽉1 1⽇",
        suggestion='改为"2025年9月11日"。',
    )
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_DOUBTFUL
    assert "空格" in _notes_text(issue)


def test_rule_j_does_not_trigger_for_genuine_typo():
    """真实案例："Linxu"应改为"Linux"——真正的拼写错误，不含版式错乱措辞，
    也不是纯空格差异，必须保持确定性错误，不能被误伤降级。"""
    raw = _raw_issue(original_text="而Linxu生态", suggestion='"Linxu"应改为"Linux"。')
    issue = classify_issue(raw, {})
    assert issue.layer == config.LAYER_CONFIRMED


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
