"""修饰规则（在基础归层结果之上叠加，非互斥）。

对应设计文档的规则C(OCR低置信度降级)/D(未定位封顶)/G(知识时效性豁免)/
H(分块边界截断豁免)/I(PDF换行转写空格伪影豁免)，以及优先级覆盖（政治敏感/民族地名）。

所有修饰规则（含不往layer_notes留痕的优先级覆盖）统一签名
(raw, block, tail_blocks, state) -> state，由 _ClassificationState 统一状态
载体 + _MODIFIER_RULES 显式有序注册表编排：想知道叠加顺序看 _MODIFIER_RULES
这一个列表就够了，不用通读整个函数体；加新规则只是往列表里插一条并写清楚为什么
插在这个位置。详细设计背景见 core/classifier/CLAUDE.md。

本模块不接受任何反馈相关参数——人工反馈改在校对提示词阶段规避，不在分类器里做
事后降级，详见 core/feedback_rules.py 模块docstring；分类器保持"纯规则逻辑，
不调用LLM、不查库"。
"""

from __future__ import annotations

from dataclasses import replace

import config
from core.chunker import ChunkedDocument
from core.classifier._types import _ClassificationState
from core.classifier.heuristics import _YEAR_RE
from core.parser import ParsedBlock
from core.proofreader import RawIssue

_OCR_DOWNGRADE_ISSUE_TYPES = ("错别字与拼写", "标点符号问题")
_SENTENCE_TERMINAL_PUNCT = "。！？"


def _apply_ocr_downgrade(
    raw: RawIssue,
    block: ParsedBlock | None,
    tail_blocks: set[int],
    state: _ClassificationState,
) -> _ClassificationState:
    if block is None or block.ocr_confidence is None:
        return state
    if block.ocr_confidence >= config.OCR_CONF_DOWNGRADE_THRESHOLD:
        return state
    if raw.issue_type not in _OCR_DOWNGRADE_ISSUE_TYPES:
        return state

    notes = state.notes + [f"低OCR置信度降级(conf={block.ocr_confidence})"]
    suggestion = "该处文字来自OCR识别且置信度较低，可能为识别误差而非原文错误。" + state.suggestion
    layer = config.LAYER_DOUBTFUL if state.layer == config.LAYER_CONFIRMED else state.layer
    return replace(state, layer=layer, suggestion=suggestion, notes=notes)


def _apply_unlocated_cap(
    raw: RawIssue,
    block: ParsedBlock | None,
    tail_blocks: set[int],
    state: _ClassificationState,
) -> _ClassificationState:
    if raw.located:
        return state
    notes = state.notes + ["原文未能定位回原稿"]
    layer = config.LAYER_DOUBTFUL if state.layer == config.LAYER_CONFIRMED else state.layer
    return replace(state, layer=layer, notes=notes)


def _is_linewrap_space_artifact(raw: RawIssue, block: ParsedBlock | None) -> bool:
    """original_text 里那个空格，是不是 PDF 换行符被 LLM 转写出来的、原文根本没有的字符。

    `native_pdf.py` 把 block 内多行用 `
` 拼接，而 PDF 按页宽换行不认汉字词边界，一个双字词
    常被从中间断开（"教师"→"教
师"）。提示词要求 original_text 逐字取自正文，但真实案例显示
    LLM 读到这类跨行词会把 `
` 转写成一个空格再引用，于是 original_text 里出现一个源文档里
    并不存在的空格，被当成"多余空格"上报（真实案例：`original_text="教务管理教 师"`、
    `suggestion="应删除空格"`，原文那个位置只有一个被拼接掉的换行符）。

    判据：original_text 含空格 **且** 带空格原样不是 `block.text` 的子串（证明这个空格不是原文
    真实携带的）**且** 去掉所有空格后是 `block.text` 去掉所有换行符后的子串（证明内容确实原样
    来自这个 block）。

    **整体去除后再子串匹配，不逐个空格去找对应的 `
` 位置**：真实案例里 LLM 插入空格的位置和
    `
` 的真实位置能差一个字符（"教务管理教 师" 的 `
` 实际在"理"和"教"之间，空格却落在
    "教"和"师"之间），它不是在做逐字符替换，更像是按对这个词的理解重新组织了一遍再输出。要求
    逐位置精确对应会漏判真实案例。

    **不要求 issue_type 必须是"错别字与拼写"**：子串匹配本身已经足够精确，用 issue_type 收窄
    反而会漏掉 LLM 把同一现象归到"标点符号问题"的场景。也不区分深度/精简模式——它修的是
    "LLM 转写出了原文里根本不存在的内容"这个正确性问题，与校对严格程度无关。
    """
    if block is None or " " not in raw.original_text:
        return False
    if raw.original_text in block.text:
        return False  # 空格是原文/解析结果里真实携带的，不是转写伪影
    return raw.original_text.replace(" ", "") in block.text.replace("\n", "")


def _has_recency_doubt_wording(text: str) -> bool:
    return any(k in text for k in config.RECENCY_DOUBT_KEYWORDS)


def _extract_years(text: str) -> list[int]:
    return [int(m[:4]) for m in _YEAR_RE.findall(text)]


def _is_recency_misjudgment(raw: RawIssue) -> bool:
    """LLM 只是因为年份超出它的训练数据覆盖范围而怀疑，不是真的发现了事实性错误。

    这种怀疑**只针对"这个时间点本身存不存在"，不针对"该时间点发生的事是否属实"**——是模型
    知识时效性的局限，不是文档的问题。

    三个条件同时成立才算：issue_type/category 落在事实类范围内、`reason`/`suggestion` 命中
    `config.RECENCY_DOUBT_KEYWORDS`（"训练数据""知识截止""较新"这类怀疑"时间太新"的措辞，不是
    泛泛的事实怀疑）、原文里最大的年份落在 `[知识截止年份, +KNOWLEDGE_CUTOFF_GRACE_YEARS]`
    区间内——超出宽限期视为真的离谱（如相差几十年），不算这一类，保留给人工核实。
    """
    if raw.category != "factual" and raw.issue_type != "常识与事实性错误":
        return False
    combined = f"{raw.reason}{raw.suggestion}"
    if not _has_recency_doubt_wording(combined):
        return False
    years = _extract_years(raw.original_text) or _extract_years(combined)
    if not years:
        return False
    max_year = max(years)
    if max_year <= config.LLM_KNOWLEDGE_CUTOFF_YEAR:
        return False
    return max_year - config.LLM_KNOWLEDGE_CUTOFF_YEAR <= config.KNOWLEDGE_CUTOFF_GRACE_YEARS


def _compute_chunk_tail_blocks(chunked: ChunkedDocument | None) -> set[int]:
    """返回"是所在chunk正文最后一个block、且该chunk不是全文档最后一个chunk"的block_index集合。

    分块的"重叠区"（config.OVERLAP_BLOCKS）只向后携带上一块内容供LLM理解上文，从未设计
    "预览下一块"——落在这类block里的内容，在其所属chunk的正文视野里就是到此为止，LLM完全
    看不到下一分块的后续内容。
    """
    if chunked is None or not chunked.chunks:
        return set()
    last_chunk_index = len(chunked.chunks) - 1
    tail_blocks: set[int] = set()
    for chunk in chunked.chunks:
        if chunk.chunk_index == last_chunk_index:
            continue  # 全文档最后一个chunk，后面没有更多分块内容，不存在"看不到后续"的问题
        if chunk.block_indices:
            tail_blocks.add(chunk.block_indices[-1])
    return tail_blocks


def _is_chunk_boundary_truncation(raw: RawIssue, block: ParsedBlock | None, tail_blocks: set[int]) -> bool:
    """LLM 报的"内容不完整/缺标点"，其实是它在分块边界处看不到后续内容造成的误判。

    `config.OVERLAP_BLOCKS` 的重叠区只让后一块向前携带上一块的尾部供理解上文，从未设计"预览
    下一块"。所以落在"所在 chunk 正文最后一个 block"里的内容，在 LLM 的视野里就是到此为止——
    它据此判断"这句话没说完"是**合理的**，不是凭空捏造，因此不能靠改提示词指望它猜到自己看漏
    了内容，只能由系统层用确定性的结构事实来识别。

    四个条件同时成立：`block_index` 在 `tail_blocks` 里（是所在 chunk 正文尾块，且该 chunk 不是
    全文档最后一块——最后一块后面确实没有内容了）**且** block 原文不以句末标点收尾（正常收尾的
    段落即使卡在边界也不该被怀疑）**且** 被 flag 的 original_text 原样是该 block 的结尾片段
    （排除"block 确实在 chunk 尾、但问题其实出在 block 中间别处"）。

    **为什么不在解析阶段把这类 block 合并回去**：诊断真实案例时量过，被拆开的两个 block 之间的
    行间距（约12pt）和同一悬挂缩进段落里"真正的新条目"之间几乎完全相同（同一份文档实测都在
    12.2~13.0pt），唯一可靠的区分信号是横坐标缩进——那是这份文档排版软件的具体几何特征，换个
    软件未必成立，而且完全不覆盖 OCR/Word 两条通道。这里用的"是不是 chunk 正文尾块"是从
    `ChunkedDocument` 直接读出的结构事实，不依赖任何排版几何假设。
    """
    if raw.block_index not in tail_blocks or block is None:
        return False
    block_text = block.text.rstrip()
    if block_text and block_text[-1] in _SENTENCE_TERMINAL_PUNCT:
        return False
    flagged = raw.original_text.strip()
    return bool(flagged) and block_text.endswith(flagged)


def is_artifact_misjudgment(raw: RawIssue, block: ParsedBlock | None, tail_blocks: set[int]) -> bool:
    """这条 issue 是不是"我们自己造成的误判"——解析伪影、模型知识边界、分块边界三选一。

    三类的共同点：**问题不在文档里，在我们这条流水线上**。整条丢弃而不是降级（用户明确要求）：
    降级会贴一句"疑似……建议核实后再处理"，那句话本身就是噪声——用户看到的仍是一条要处理的
    问题，而它百分之百不是原文的错。

    判定在归层**之前**，所以不看 layer，引文类/风格类也一并丢。不违反引文保护铁律——那条
    要的是"不对引文提改动建议"，这里是把一条压根不存在的问题整个删掉，比"原文照录"更保守。
    """
    return (
        _is_recency_misjudgment(raw)
        or _is_linewrap_space_artifact(raw, block)
        or _is_chunk_boundary_truncation(raw, block, tail_blocks)
    )


def _apply_priority_override(
    raw: RawIssue,
    block: ParsedBlock | None,
    tail_blocks: set[int],
    state: _ClassificationState,
) -> _ClassificationState:
    """优先级覆盖：政治敏感性表述/民族与地名规范，无论落在哪层都强制最高优先级。

    重构前这是 classify_issue 末尾一条裸的 if 语句，是唯一不往 layer_notes 留痕的
    地方——和"每条规则都要在notes里记录依据"这个约定不一致。折进注册表时补上记录，
    不改变判定条件本身。
    """
    if raw.issue_type not in config.HIGH_PRIORITY_ISSUE_TYPES:
        return state
    notes = state.notes + [f"issue_type={raw.issue_type}命中高优先级强制覆盖"]
    return replace(state, priority=config.PRIORITY_HIGH, notes=notes)


# 显式声明修饰规则的执行顺序——想知道"规则G/H谁先跑"看这一个列表就够了，不用
# 通读 classify_issue 的函数体。以后加新规则：写一个同签名函数，在这里插入一条，
# 并写清楚为什么插在这个位置（是否依赖前面某条规则已经改过layer/priority）。
_MODIFIER_RULES = (
    ("OCR低置信度降级(规则C)", _apply_ocr_downgrade),
    ("未定位封顶(规则D)", _apply_unlocated_cap),
    ("高优先级覆盖(政治敏感/民族地名，与layer无关，放最后)", _apply_priority_override),
)
