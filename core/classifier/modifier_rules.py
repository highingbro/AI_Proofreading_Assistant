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
from core.classifier.postprocess import _extract_replacement_pair, _normalize_lookalike, _strip_whitespace
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


def _apply_linewrap_space_artifact_downgrade(
    raw: RawIssue,
    block: ParsedBlock | None,
    tail_blocks: set[int],
    state: _ClassificationState,
) -> _ClassificationState:
    """规则I：PDF换行符被LLM转写成空格的解析伪影豁免。

    起因：core/parser/native_pdf.py 用换行符(\\n)拼接同一block内的多个PDF行（见
    core/parser/CLAUDE.md），但PDF按页宽自动换行不认汉字词语边界，经常把一个双字词从
    中间断开（如"教师"被拆成上一行末尾的"教"、下一行开头的"师"），block.text里就会出现
    "...教\\n师..."这种词语中间夹着换行符的情况。LLM被要求"original_text必须逐字取自
    正文"，但真实案例显示它读到这类跨行的词语时会把\\n转写/归一化成一个空格再引用（而
    不是照原样保留\\n，或者干脆去掉\\n直接拼接），导致original_text里出现一个源文档里
    其实并不存在的空格，进而被当成"多余空格/错别字"上报——这是转写伪影，不是原文真的
    有这个空格或缺这个空格。

    判定条件：state.layer是"确定性错误"（比这更宽松的层级不需要再降）+ original_text
    含空格 + original_text本身（带空格）不是block.text的子串（证明这个空格不是原文/
    解析结果真实携带的）+ original_text去掉所有空格后是block.text去掉所有换行符后的
    子串（证明"去掉这些空格/换行符差异，内容确实原样来自block"）。要求整体去除后再
    子串匹配，而不是逐个空格找\\n位置一一对应替换——真实案例验证过LLM转写时插入空格的
    具体位置和block里\\n的真实位置可能相差一个字符（不是精确的\\n→空格单点替换，更像是
    LLM按语义整体重新组织了一下），要求逐位置精确对应会漏判真实案例。
    """
    if state.layer != config.LAYER_CONFIRMED:
        return state
    if block is None or " " not in raw.original_text:
        return state
    if raw.original_text in block.text:
        return state  # 空格是原文/解析结果里真实携带的，不是转写伪影，不豁免
    despaced = raw.original_text.replace(" ", "")
    flattened = block.text.replace("\n", "")
    if despaced not in flattened:
        return state  # 去除空格/换行后也对不上原文，不是这类伪影，保留原判定

    notes = state.notes + ["original_text中的空格疑似PDF换行符被LLM转写成空格的伪影，原文中并无此空格，降级为存疑待核实"]
    suggestion = (
        "该处空格疑似因PDF内部排版换行被误转写产生，原文中并无此空格，建议对照原文核实后再处理：" + state.suggestion
    )
    return replace(state, layer=config.LAYER_DOUBTFUL, priority=config.PRIORITY_LOW, suggestion=suggestion, notes=notes)


def _apply_layout_and_space_artifact_downgrade(
    raw: RawIssue,
    block: ParsedBlock | None,
    tail_blocks: set[int],
    state: _ClassificationState,
) -> _ClassificationState:
    """规则J：解析伪影兜底降级——版式错乱措辞 / 建议与原文仅空格差异。

    起因：即使 core/classifier/postprocess.py 的丢弃过滤器（_filter_visually_no_op）
    已经拦掉大部分部首/异体字替代型和控制字符乱码型假问题，真实数据（data/app.db
    35号记录）显示仍有残留：有的suggestion只描述了original_text里某个片段的替换、
    但那个片段不足以支撑discard过滤器判定"整条零改动"；有的LLM会自己在reason/
    suggestion里承认这是"排版错乱""跨行错位""乱码"（PDF多栏/表格/图注版式被解析
    打乱语序、拼接错行），这不是语言本身的错误，只是LLM也没看懂被打乱的版面；有的
    建议和原文的唯一差异是空格数量（装饰性字间距、跨行拼接等排版原因导致，原文是否
    真的多/少这个空格无法仅凭文本判断）。三种情况的共同点——都不是"一眼就能看出"的
    确定性错误，必须至少降级为存疑，不能留在错误类。

    在"确定性错误"或"存疑待核实"时都生效——不是只认前者。真实案例：某条issue
    先被规则D（未定位）从确定性错误封顶降到存疑待核实，此时如果本规则仍然只认
    `state.layer == 确定性错误`，会直接跳过，结果这条issue最终停在"存疑待核实/
    中优先级"，既没有优先级降到最低，也没留下"这其实是纯空格差异"这条更有价值
    的具体诊断——用户只看得到"未定位"，看不出这条根本不必核实。已经落在引文类/
    风格类的issue（各自有自己的处理逻辑）才真正不需要本规则再插手。命中后优先级
    强制改判最低（哪怕当前已经是存疑，也可能还停在未改判的中优先级），layer方面
    如果当前已经是存疑待核实则保持不变，不会往回提升。

    空格差异判定要先套一遍 _normalize_lookalike 再比较（而不是直接比较原始文本
    去空格），是因为真实案例里空格差异经常和部首替代字同时出现在同一条issue里
    （如"2 0 2 5年9⽉1 1⽇"→"2025年9月11日"，⽉是部首替代字、其余是空格差异），
    只看原始文本去空格会因为部首字符不相等而漏判。
    """
    if state.layer not in (config.LAYER_CONFIRMED, config.LAYER_DOUBTFUL):
        return state

    combined = f"{raw.reason}{raw.suggestion}"
    if any(k in combined for k in config.LAYOUT_ARTIFACT_KEYWORDS):
        notes = state.notes + ["建议措辞疑似描述版式错乱/乱码，降级为存疑待核实"]
        suggestion = (
            "该问题疑似由文档排版错乱或解析乱码导致，不是确定性的语言错误，"
            "建议对照原文核实后再处理：" + state.suggestion
        )
        return replace(state, layer=config.LAYER_DOUBTFUL, priority=config.PRIORITY_LOW, suggestion=suggestion, notes=notes)

    pair = _extract_replacement_pair(raw)
    if pair is not None:
        old, new = pair
        has_real_space_diff = _strip_whitespace(old) != old.strip() or _strip_whitespace(new) != new.strip()
        if has_real_space_diff and _strip_whitespace(_normalize_lookalike(old)) == _strip_whitespace(_normalize_lookalike(new)):
            notes = state.notes + ["建议与原文仅空格差异，疑似解析产生的空白伪影，降级为存疑待核实"]
            suggestion = (
                "该问题与原文的差异只有空格，疑似解析产生的空白伪影而非原文真实错误，"
                "建议对照原文核实后再处理：" + state.suggestion
            )
            return replace(state, layer=config.LAYER_DOUBTFUL, priority=config.PRIORITY_LOW, suggestion=suggestion, notes=notes)

    return state


def _has_recency_doubt_wording(text: str) -> bool:
    return any(k in text for k in config.RECENCY_DOUBT_KEYWORDS)


def _extract_years(text: str) -> list[int]:
    return [int(m[:4]) for m in _YEAR_RE.findall(text)]


def _apply_recency_downgrade(
    raw: RawIssue,
    block: ParsedBlock | None,
    tail_blocks: set[int],
    state: _ClassificationState,
) -> _ClassificationState:
    """规则G：LLM单纯因知识时效性（年份超出训练数据覆盖范围）而怀疑，不是真的事实性错误。

    只在 issue_type/category 落在"事实类"范围内、reason或suggestion里出现"怀疑年份太新"
    的措辞、且原文里最大的年份落在[知识截止年份, 知识截止年份+容忍宽限期]区间时才生效——
    超过宽限期视为真的离谱（如相差几十年），不豁免，保留原判定供人工核实。
    """
    if raw.category != "factual" and raw.issue_type != "常识与事实性错误":
        return state

    combined = f"{raw.reason}{raw.suggestion}"
    if not _has_recency_doubt_wording(combined):
        return state

    years = _extract_years(raw.original_text) or _extract_years(combined)
    if not years:
        return state

    max_year = max(years)
    if max_year <= config.LLM_KNOWLEDGE_CUTOFF_YEAR:
        return state  # 年份没超过知识截止，不属于这条豁免场景
    if max_year - config.LLM_KNOWLEDGE_CUTOFF_YEAR > config.KNOWLEDGE_CUTOFF_GRACE_YEARS:
        return state  # 超出宽限期太多，视为真的离谱，不豁免

    notes = state.notes + [
        f"疑似因LLM知识时效性(截止{config.LLM_KNOWLEDGE_CUTOFF_YEAR}年)导致的误判"
        f"(原文年份{max_year}，未超过{config.KNOWLEDGE_CUTOFF_GRACE_YEARS}年宽限期)，降级为存疑待核实+最低优先级"
    ]
    suggestion = "该问题疑似因AI知识时效性（训练数据未覆盖到这一时间点）产生的误判，建议直接忽略，除非另有证据：" + state.suggestion
    return replace(state, layer=config.LAYER_DOUBTFUL, priority=config.PRIORITY_LOW, suggestion=suggestion, notes=notes)


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


def _apply_chunk_boundary_downgrade(
    raw: RawIssue,
    block: ParsedBlock | None,
    tail_blocks: set[int],
    state: _ClassificationState,
) -> _ClassificationState:
    """规则H：分块(chunk)边界截断误判豁免。

    起因：真实文档里一段悬挂缩进排版的列表被PyMuPDF拆成了两个ParsedBlock，又恰好被分块
    算法切在两个不同chunk里——LLM校对前一个chunk时，正文原文在此处硬生生截断，看到的就是
    一段"戛然而止"的文本，把"分块导致看不到后续"误判成"内容不完整/标点缺失"。

    命中条件：issue所在block是它所在chunk正文的最后一个block（且该chunk不是全文档最后一
    个chunk）+ block文本本身不以句末标点收尾（看起来像被截断，不是正常段落收尾）+ 被flag
    的原文原样出现在该block的结尾处（避免"block确实在chunk尾但issue其实在block中间"的误伤）。
    只在基础归层已判定为"确定性错误"时才降级——存疑/引文/风格本身已经比确定性错误宽松。
    """
    if state.layer != config.LAYER_CONFIRMED:
        return state
    if raw.block_index not in tail_blocks:
        return state
    if block is None:
        return state

    block_text = block.text.rstrip()
    if block_text and block_text[-1] in _SENTENCE_TERMINAL_PUNCT:
        return state  # block本身以句末标点正常收尾，不像是被截断的
    flagged = raw.original_text.strip()
    if not flagged or not block_text.endswith(flagged):
        return state  # 被flag的原文不是该block的结尾片段，不是这次要拦的场景

    notes = state.notes + [
        "疑似因分块(chunk)边界截断导致的误判：该block是所在分块正文的最后一块，"
        "且不以句末标点收尾，LLM校对时未看到后续分块内容"
    ]
    suggestion = (
        "该问题疑似因文档分块处理、AI在此处未能看到后续分块内容而产生的误判"
        "（原文在此处被分块边界截断，并非原文真的不完整），建议对照原文续接内容核实后再处理：" + state.suggestion
    )
    return replace(state, layer=config.LAYER_DOUBTFUL, priority=config.PRIORITY_LOW, suggestion=suggestion, notes=notes)


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
    ("PDF换行符转写成空格的伪影豁免(规则I)", _apply_linewrap_space_artifact_downgrade),
    ("版式错乱/空格差异兜底降级(规则J)", _apply_layout_and_space_artifact_downgrade),
    ("知识时效性豁免(规则G，依赖前面规则已判定的层级)", _apply_recency_downgrade),
    ("分块边界截断豁免(规则H，只在仍是确定性错误时生效，需在G之后跑)", _apply_chunk_boundary_downgrade),
    ("高优先级覆盖(政治敏感/民族地名，与layer无关，放最后)", _apply_priority_override),
)
