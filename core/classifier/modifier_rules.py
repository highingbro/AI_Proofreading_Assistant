"""修饰规则（在基础归层结果之上叠加，非互斥）。

对应设计文档的规则C(OCR低置信度降级)/D(未定位封顶)/G(知识时效性豁免，补丁)/
H(分块边界截断豁免，补丁)/I(PDF换行转写空格伪影豁免，补丁)/J(人工反馈学习自动
降级，补丁)，以及优先级覆盖（政治敏感/民族地名）。

补丁（重构，非阶段5原始设计）：原实现是 classify_issue 里手写的四行连续函数调用，
每个函数签名都不一样（有的碰priority有的不碰），叠加顺序完全靠这四行的书写顺序
隐式表达，没有任何地方声明"规则G必须在H之前跑"。改成 _ClassificationState 统一
状态载体 + _MODIFIER_RULES 显式有序注册表：所有修饰规则（含原来连名字都没有、
也不往layer_notes留痕的优先级覆盖）统一签名 (raw, block, tail_blocks, state,
learned_feedback) -> state，想知道叠加顺序看 _MODIFIER_RULES 这一个列表就够了，
不用通读整个函数体；以后加新规则只是往列表里插一条并写清楚为什么插在这个位置。
详细设计背景见 core/classifier/CLAUDE.md。

learned_feedback 参数（阶段12新增）只有规则J真正使用，其余规则忽略即可——与
base_rules.py当初给所有基础规则统一加宽 mode 参数是同一惯例，不是这里新发明的风格。
"""

from __future__ import annotations

from dataclasses import replace

import config
from core.chunker import ChunkedDocument
from core.classifier._types import _ClassificationState
from core.classifier.heuristics import _YEAR_RE, _has_factual_feature
from core.feedback import LearnedFeedback, count_similar_rejections
from core.parser import ParsedBlock
from core.proofreader import RawIssue

_OCR_DOWNGRADE_ISSUE_TYPES = ("错别字与拼写", "标点符号问题")
_SENTENCE_TERMINAL_PUNCT = "。！？"
_FEEDBACK_EXEMPT_ISSUE_TYPES = ("常识与事实性错误",)


def _apply_ocr_downgrade(
    raw: RawIssue,
    block: ParsedBlock | None,
    tail_blocks: set[int],
    state: _ClassificationState,
    learned_feedback: list[LearnedFeedback],
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
    learned_feedback: list[LearnedFeedback],
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
    learned_feedback: list[LearnedFeedback],
) -> _ClassificationState:
    """规则I（补丁，真实使用中发现后追加）：PDF换行符被LLM转写成空格的解析伪影豁免。

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


def _has_recency_doubt_wording(text: str) -> bool:
    return any(k in text for k in config.RECENCY_DOUBT_KEYWORDS)


def _extract_years(text: str) -> list[int]:
    return [int(m[:4]) for m in _YEAR_RE.findall(text)]


def _apply_recency_downgrade(
    raw: RawIssue,
    block: ParsedBlock | None,
    tail_blocks: set[int],
    state: _ClassificationState,
    learned_feedback: list[LearnedFeedback],
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
    learned_feedback: list[LearnedFeedback],
) -> _ClassificationState:
    """规则H（补丁，真实使用中发现后追加）：分块(chunk)边界截断误判豁免。

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


def _is_factual_issue(raw: RawIssue) -> bool:
    """事实类判定口径，与 _rule_factual（base_rules.py）完全一致的三个信号。

    规则J（下面）用它来豁免事实类问题——不能只查 category/issue_type 两个信号，
    否则会漏掉"LLM没自报factual、但命中了人名/职务/机构/年份特征启发式"这一类，
    达不到"事实类问题永不自动降级"的设计要求。
    """
    return (
        raw.category == "factual"
        or raw.issue_type in _FEEDBACK_EXEMPT_ISSUE_TYPES
        or _has_factual_feature(raw)
    )


def _apply_learned_feedback_downgrade(
    raw: RawIssue,
    block: ParsedBlock | None,
    tail_blocks: set[int],
    state: _ClassificationState,
    learned_feedback: list[LearnedFeedback],
) -> _ClassificationState:
    """规则J（补丁，阶段12新增）：同一类问题被人工反复拒绝达到阈值后，自动降级为风格可选。

    起因：用户实测发现，同一类被人工判定"判错了"的问题（点击"拒绝"）会在后续校对里
    反复出现，需要重新拒绝一遍。真实例子：原文"管理控台一组织权限一用户管理"，
    issue_type=错别字与拼写，suggestion="将'一'改为'-'或'>'等规范的路径分隔符"——这是
    没有意义的改动，用户每次都会拒绝。

    判定条件：同 issue_type 前提下，历史反馈里 original_text 精确匹配或 suggestion
    相似度（core/feedback.py::count_similar_rejections，用 difflib，不引入语义模型）
    达到阈值的累计命中次数达到 config.FEEDBACK_REJECTION_THRESHOLD 才生效，避免手滑
    拒绝一次就永久压掉一类问题。

    豁免：引文层(LAYER_QUOTATION)和已经是风格可选(LAYER_OPTIONAL)的问题不处理（前者
    是独立于"风格可选"的保护层级，语义不同，不应被这条规则改写；后者已经到底不需要
    重复处理）；事实类问题（_is_factual_issue）永久不参与——即使被反复拒绝次数远超
    阈值也不降级，这是设计铁律"事实性内容必须始终保留人工复核机会"的延伸，已与用户
    确认（见 core/classifier/CLAUDE.md 规则J一节）。

    放在 _MODIFIER_RULES 里其余降级规则(C/D/I/G/H)之后、优先级覆盖之前——这条规则要
    看的是"系统层其它规则都处理完之后的最终层级"，人工反馈的降级判断不应该被其他规则
    的处理顺序打断。这是第一条会把 layer 一路降到 LAYER_OPTIONAL 的 modifier 规则
    （C/D/G/H/I 都只把 LAYER_CONFIRMED 封顶降到 LAYER_DOUBTFUL），详见
    core/classifier/CLAUDE.md 里对这一点的架构提醒。
    """
    if state.layer in (config.LAYER_QUOTATION, config.LAYER_OPTIONAL):
        return state
    if _is_factual_issue(raw):
        return state
    if not learned_feedback:
        return state
    count = count_similar_rejections(raw.issue_type, raw.original_text, raw.suggestion, raw.reason, learned_feedback)
    if count < config.FEEDBACK_REJECTION_THRESHOLD:
        return state

    notes = state.notes + [
        f"该类问题此前已被人工拒绝{count}次(阈值{config.FEEDBACK_REJECTION_THRESHOLD})，自动降级为风格可选"
    ]
    suggestion = f"系统提示：此类问题已被人工多次拒绝({count}次)，自动归为风格可选，仅供参考：" + state.suggestion
    return replace(
        state, layer=config.LAYER_OPTIONAL, priority=config.PRIORITY_OPTIONAL, suggestion=suggestion, notes=notes
    )


def _apply_priority_override(
    raw: RawIssue,
    block: ParsedBlock | None,
    tail_blocks: set[int],
    state: _ClassificationState,
    learned_feedback: list[LearnedFeedback],
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
    ("知识时效性豁免(规则G，依赖前面规则已判定的层级)", _apply_recency_downgrade),
    ("分块边界截断豁免(规则H，只在仍是确定性错误时生效，需在G之后跑)", _apply_chunk_boundary_downgrade),
    ("人工反馈学习自动降级(规则J，需在其余降级规则之后跑，看最终态)", _apply_learned_feedback_downgrade),
    ("高优先级覆盖(政治敏感/民族地名，与layer无关，放最后)", _apply_priority_override),
)
