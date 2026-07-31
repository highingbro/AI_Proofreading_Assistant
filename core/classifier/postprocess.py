"""跨块去重 / 排序 / 统计 / 视觉无实质改动的建议过滤。"""

from __future__ import annotations

import difflib
import logging
import re
import unicodedata

import config
from core.classifier._types import ClassifiedIssue
from core.proofreader import RawIssue

logger = logging.getLogger(__name__)

# 只锚定"应改为/改为『X』"这个确定性改写措辞去找引号内文字，不是任意引号——"存疑，
# 建议人工核实：..."这类suggestion按提示词补充规则二的固定格式，惯例上会在句子里
# 重新引用原文做说明（如"...称『年节约成本超过50万元』..."），套用宽泛的"找引号"
# 规则会把这类正常的存疑issue误伤成"零改动"，见 core/classifier/CLAUDE.md。
_REPLACEMENT_SUGGESTION_RE = re.compile(r'(?:应改为|改为)[“"\']([^”"\']+)[”"\']')

# 整段替换建议里，被替换的整段本身可能含引号（LLM转述原文时会把原文自带的引号一起带上，
# 如 应改为“走进…深入“全员自主改善”的现场…”）。此时上面那条非贪婪规则会在第一个内嵌
# 引号处截断，抽出残缺的新文本，长度对不上原文，零改动判定随即失效——真实案例是一批
# 部首类假错误因此漏过过滤。这条改用贪婪匹配把整段取全，并要求引号收在建议末尾（允许尾随
# 句号），避免在"改为『X』，因为『Y』"这类后面还有引用的措辞里抽过头。
_REPLACEMENT_TO_END_RE = re.compile(r'(?:应改为|改为)[“"\'](.+)[”"\']\s*[。．.]?\s*$', re.S)

# "『旧片段』应改为『新片段』"这种只描述original_text里某个具体片段替换的措辞（常见于
# suggestion只想指出一个字/词有问题，而不是整个original_text都要换掉）——
# _REPLACEMENT_SUGGESTION_RE 那种"整段替换"比较方式在这种措辞下会因为新旧片段长度和
# original_text整体长度不一致而误判成"不是零改动"，需要单独识别这一对片段直接比较，
# 见 core/classifier/CLAUDE.md。
_FRAGMENT_REPLACEMENT_RE = re.compile(r'[“"\']([^”"\']+)[”"\'](?:应改为|应为|改为)[“"\']([^”"\']+)[”"\']')

# 剔除引文类issue的"疑点供参考"文本（见 _strip_visually_no_op_fragments）时，连同片段
# 前面的连接词一起去掉，避免剔除后留下"……，且"这种悬空的残句。
_FRAGMENT_WITH_LEADING_CONNECTOR_RE = re.compile(
    r"(?:[，,、]\s*(?:且|并且|同时)\s*)?" + _FRAGMENT_REPLACEMENT_RE.pattern
)

# 字形变体码位区间：这些区块里的字符肉眼与规范汉字无异，码位却不是CJK统一汉字。
# PDF字体的ToUnicode CMap有缺陷时会把正文汉字映射到这些码位——真实文档实测（一份19页
# 刊物）有2122个，LLM会把它们整段当错别字报出来（一次71条问题里50条因此而来），且落进
# 置信度最高的"错误类"。
_CJK_VARIANT_RANGES = (
    (0x2E80, 0x2EF3),    # CJK部首补充：整个区块都没有NFKC兼容分解，只能靠码位区间识别
    (0x2F00, 0x2FD5),    # 康熙部首：有兼容分解，但分解结果可能是繁体，不能只靠NFKC比对
    (0xF900, 0xFAFF),    # CJK兼容汉字
    (0x2F800, 0x2FA1D),  # CJK兼容汉字补充
)

# 康熙部首是**传统**部首，NFKC分解结果一律是繁体字形，而简体正文里这些字符代表的是
# 简体字（`⼾`U+2F3E分解成`戶`，正文里其实是`户`）。这张表把分解结果折回简体，供
# _variant_char_matches 判定"变体字符与建议里的规范字是否同一个字"。
# **它和"部首→汉字"那种映射表不是一回事，边界是封闭的**：康熙部首区固定214个字符，
# 其中分解结果简繁有别的就这么些，一次收全即可，不存在"下一份文档又冒出新字符"的问题；
# 而且漏收只会让个别建议少丢一条（保守方向），不会造成误删。
_KANGXI_TRADITIONAL_FOLDINGS = {
    "戶": "户", "門": "门", "馬": "马", "車": "车", "頁": "页", "見": "见",
    "貝": "贝", "風": "风", "長": "长", "齒": "齿", "龜": "龟", "黽": "黾",
    "麥": "麦", "黃": "黄", "韋": "韦", "飛": "飞", "魚": "鱼", "鳥": "鸟",
    "龍": "龙", "齊": "齐", "鹵": "卤", "語": "语", "貞": "贞", "隸": "隶",
}

_CONSERVATISM_RANK = {
    config.LAYER_QUOTATION: 3,
    config.LAYER_DOUBTFUL: 2,
    config.LAYER_OPTIONAL: 1,
    config.LAYER_CONFIRMED: 0,
}

_STATS_LAYER_FIELD = {
    config.LAYER_CONFIRMED: "count_confirmed",
    config.LAYER_DOUBTFUL: "count_doubtful",
    config.LAYER_QUOTATION: "count_quotation",
    config.LAYER_OPTIONAL: "count_optional",
}


def _normalize_for_dedup(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _strip_whitespace(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _normalize_lookalike(text: str) -> str:
    """NFKC兼容规范化，识别全角/半角、兼容字形这类"视觉等价但码位不同"的零改动建议。

    字形变体（部首区/兼容汉字区）不在这里处理，交给 _diff_is_only_cjk_variants——
    NFKC对CJK部首补充区整个区块都无效，且对康熙部首的分解结果可能是繁体，靠它比对
    会漏判。
    """
    return unicodedata.normalize("NFKC", text)


def _is_cjk_variant_char(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_VARIANT_RANGES)


def _variant_char_matches(old_ch: str, new_ch: str) -> bool:
    """old_ch 是字形变体字符，且它与 new_ch 确实是同一个字。

    必须校验"old_ch 的规范形式是否就是 new_ch"，不能只看 old_ch 落在变体区就放行——
    真实反例：`数⼦化`→`数字化` 里 `⼦`(康熙部首"子") 被误用成了`字`，这是**真错别字**，
    不校验就会被当成字形变体丢掉。

    三种放行情形：
    1. 没有NFKC兼容分解（CJK部首补充区整块如此）——无从校验，按"该区字符本就是某个
       汉字的部首形式"放行。这是不维护映射表的代价，换来对任意没见过的字符都生效。
    2. 分解结果就是 new_ch——最常见的情形。
    3. 分解结果是 new_ch 的繁体（_KANGXI_TRADITIONAL_FOLDINGS）——康熙部首区是**传统**
       部首，分解结果一律繁体字形，而简体正文里它代表简体字。
    """
    if not _is_cjk_variant_char(old_ch):
        return False
    folded = unicodedata.normalize("NFKC", old_ch)
    if folded == old_ch:
        return True
    return folded == new_ch or _KANGXI_TRADITIONAL_FOLDINGS.get(folded) == new_ch


def _diff_is_only_cjk_variants(old: str, new: str) -> bool:
    """old→new 的全部差异是否都只是"字形变体字符换成规范汉字"。

    **不依赖任何字符映射表**：只判断差异位置上 old 侧的字符码位是否落在变体区，不关心
    它具体对应哪个汉字。这一点是关键——早先的做法是手工维护一张"部首→汉字"映射表，
    只能覆盖已收录的字符，每换一份新文档就可能撞上表外的新字符：真实教训是那张13字表
    被 `⻣`(骨)、`⻰`(龙) 破防，一次放出38条假错别字，且这类字符散布在整句里，LLM会
    引用整段来报，原文长度和噪声量都成倍上升。按码位区间判断则对任意文档、任意没见过
    的变体字符都成立，不需要维护表，也不会因为映射写错而静默篡改比对结果。

    只认等长替换：出现增删说明改的是内容本身，不是单纯的字形替换，必须放行给人工判断
    （如 `面对们`→`面对面` 是真错别字，`⸺`→`——` 是真的破折号字符不对，两者都不该丢）。
    """
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag != "replace" or (i2 - i1) != (j2 - j1):
            return False
        if not all(_variant_char_matches(a, b) for a, b in zip(old[i1:i2], new[j1:j2])):
            return False
    return True


def _fragment_is_only_cjk_variants(original: str, proposed: str) -> bool:
    """suggestion 只给出原文里某个词的规范写法时，在原文里找与它等长、且只差字形变体
    的窗口。

    LLM 常常引用一整行原文当 original_text，却只在建议里写出要改的那个词（如原文
    "能⼒，直接决定了企业的市场竞争⼒。尤其是从事⼤"、建议"应改为『能力』"）。这种
    措辞既不符合 _FRAGMENT_REPLACEMENT_RE 的"『旧』应改为『新』"格式，整段比对时长度
    又对不上，两条既有规则都接不住，而它恰恰是字形变体假错误最常见的形态。
    """
    n = len(proposed)
    if not 0 < n < len(original):
        return False
    for i in range(len(original) - n + 1):
        window = original[i:i + n]
        if window != proposed and _diff_is_only_cjk_variants(window, proposed):
            return True
    return False


def _extract_replacement_pair(raw: RawIssue) -> tuple[str, str] | None:
    """从suggestion里抽取"旧文本, 新文本"这一对，供零改动判定/空格差异判定共用。

    优先尝试"『旧』应改为『新』"片段式措辞（更具体，能处理original_text引用了更大
    上下文、真正的改动只是其中一个字/词的情况——比如original_text是"归⺟扣⾮净利润"
    整句，suggestion只说"『⺟』应改为『母』"）；抽取到的『旧』还要求在original_text
    里确实出现过，避免措辞不规范时抽到不相关的引用文本。抽不到片段式的再退回
    "应改为『新』"整段替换式措辞，与完整original_text比较。两种都抽不到返回None。
    """
    suggestion = raw.suggestion or ""
    original = (raw.original_text or "").strip()
    if not original:
        return None

    fragment_match = _FRAGMENT_REPLACEMENT_RE.search(suggestion)
    if fragment_match:
        old, new = fragment_match.group(1).strip(), fragment_match.group(2).strip()
        if old and new and old in original:
            return old, new

    # 先试"引号收在建议末尾"的贪婪匹配，能把含内嵌引号的整段完整取出；取不到再退回
    # 非贪婪版本（措辞不规范、引号没收在末尾时仍能抽到一个候选）
    for pattern in (_REPLACEMENT_TO_END_RE, _REPLACEMENT_SUGGESTION_RE):
        whole_match = pattern.search(suggestion)
        if whole_match:
            proposed = whole_match.group(1).strip()
            if proposed:
                return original, proposed

    # 兜底：以上三种措辞模式都没抽到时，把suggestion整体当成"裸"替换文本本身——真实
    # 案例：LLM有时不套"应改为『X』"这层话术，suggestion字段就是修正后的文本本身
    # （如 original_text="AI 助力PMC实战进阶"，suggestion="AI助力PMC实战进阶"，没有
    # 任何引号/动词包裹）。这种测不到的新措辞每出现一种就要新增一条正则去抽，是在
    # 追着LLM的措辞打地鼠；改成"抽不到任何已知模式就直接拿suggestion本身当候选"，
    # 后续判据（NFKC归一化/CJK变体diff/去空格比较）本身要求候选与original高度一致
    # 才会命中，真正的描述性建议（如"建议删除'的'字，或调整语序"）内容和original相差
    # 悬殊，不会被这几条判据误判，不需要再额外识别"这是不是裸替换文本"。
    proposed = suggestion.strip()
    if proposed:
        return original, proposed

    return None


def _strip_visually_no_op_fragments(text: str) -> str:
    """从自由文本（引文类issue展示给用户的"疑点供参考"原文，即 raw.reason）里剔除
    "『旧』应为/应改为『新』"这类片段式表述中，新旧文本只是CJK变体字形/兼容字形差异的
    那一部分。

    真实案例：书名号命中规则A引文保护、"原文照录不建议改动"，但LLM把两个疑点写在同一句
    reason里——"编号与标题之间的标点格式不统一，应为'4.'"是真疑点，"'⼊表'应为'入表'"
    纯粹是这份PDF的部首编码伪影（⼊是"入"的康熙部首变体，肉眼无异），却被原样展示成
    "疑点供参考"，误导核实方向。`_filter_visually_no_op` 只检查 `raw.suggestion` 整条是否
    零改动、命中就整条丢弃——但规则A展示给用户的是 `raw.reason`，且这里两个疑点混在同一
    句话里，不能整条丢弃（会连真疑点一起丢），只能剔除其中零改动的那一小段，判据与
    `_is_visually_no_op_suggestion` 共用（`_normalize_lookalike`/`_diff_is_only_cjk_variants`），
    保持两处判断标准一致。
    """
    def _replace(match: re.Match) -> str:
        old, new = match.group(1).strip(), match.group(2).strip()
        if old and new and (
            _normalize_lookalike(old) == _normalize_lookalike(new)
            or _diff_is_only_cjk_variants(old, new)
        ):
            return ""
        return match.group(0)

    return _FRAGMENT_WITH_LEADING_CONNECTOR_RE.sub(_replace, text).strip()


def _dedup(issues: list[ClassifiedIssue]) -> tuple[list[ClassifiedIssue], int]:
    """按 (block_index, 归一化original_text) 去重，保留更保守的一条。

    located=False 的条目没有可靠的 block_index，不参与去重，原样全部保留。
    """
    kept: dict[tuple[int, str], ClassifiedIssue] = {}
    order: list[tuple[int, str]] = []
    unlocated: list[ClassifiedIssue] = []
    dropped = 0

    for issue in issues:
        if issue.block_index is None:
            unlocated.append(issue)
            continue
        key = (issue.block_index, _normalize_for_dedup(issue.original_text))
        if key not in kept:
            kept[key] = issue
            order.append(key)
            continue
        existing = kept[key]
        dropped += 1
        if _CONSERVATISM_RANK[issue.layer] > _CONSERVATISM_RANK[existing.layer]:
            logger.info("去重: 用更保守的条目替换 block_index=%d 的重复问题: %s", issue.block_index, issue.original_text)
            kept[key] = issue
        else:
            logger.info("去重: 丢弃 block_index=%d 的重复问题: %s", issue.block_index, issue.original_text)

    result = [kept[k] for k in order] + unlocated
    return result, dropped


def _sort(issues: list[ClassifiedIssue]) -> list[ClassifiedIssue]:
    """block_index 本身已是 core/parser/ 按阅读顺序分配的全局序号，升序排列即满足
    "页码升序,同页按block_index"；未定位(None)的排最后。"""
    return sorted(issues, key=lambda i: (i.block_index is None, i.block_index if i.block_index is not None else 0))


def _compute_stats(issues: list[ClassifiedIssue]) -> dict:
    """字段名与 db/database.py 里 records 表列名对齐。"""
    stats = {
        "total_issues": len(issues),
        "count_confirmed": 0,
        "count_doubtful": 0,
        "count_quotation": 0,
        "count_optional": 0,
        "high_priority_count": 0,
    }
    for issue in issues:
        stats[_STATS_LAYER_FIELD[issue.layer]] += 1
        if issue.priority == config.PRIORITY_HIGH:
            stats["high_priority_count"] += 1
    return stats


def _is_visually_no_op_suggestion(raw: RawIssue) -> bool:
    """抽取出的"旧→新"文本（见 _extract_replacement_pair）实际上零改动，两条判据取或：

    1. NFKC规范化后完全相同（_normalize_lookalike）——要么字面就一样（LLM把"存疑"错报
       成确定性建议却没给出真实改动），要么只差全角/半角这类兼容字形。
    2. 差异全部落在字形变体字符上（_diff_is_only_cjk_variants）——如PDF解析产生的康熙
       部首"⽉"代替标准汉字"月"、CJK部首补充区的"⻔"代替"门"，肉眼看不出区别。
    3. suggestion只给出原文里某个词的规范写法时，原文里存在只差字形变体的等长窗口
       （_fragment_is_only_cjk_variants）——LLM引用整行原文却只写要改的那个词，是这类
       假错误最常见的形态，前两条都接不住。

    真实案例清单见 core/classifier/CLAUDE.md。
    """
    pair = _extract_replacement_pair(raw)
    if pair is None:
        return False
    old, new = pair
    if not old or not new:
        return False
    if _normalize_lookalike(old) == _normalize_lookalike(new):
        return True
    if _diff_is_only_cjk_variants(old, new):
        return True
    return _fragment_is_only_cjk_variants(old, new)


def _has_stray_control_chars(text: str) -> bool:
    """original_text里出现\\t\\n\\r之外的控制字符（Unicode Cc类），只可能是PDF字体/
    字形解析错位产生的乱码（如私有区符号被误解析成SOH等控制码）——真实文档正文不会
    包含这类字符，不是"建议本身错了"，是这条issue引用的原文字段本身就是解析垃圾，
    没有可核实的价值，直接丢弃，不必进入归层/降级流程。
    """
    return any(unicodedata.category(ch) == "Cc" and ch not in "\t\n\r" for ch in text)


def _is_layout_or_space_artifact(raw: RawIssue) -> bool:
    """这条issue是不是"解析伪影"而非原文真错：版式错乱措辞，或建议与原文只差空格。

    两种形态都来自真实数据：

    1. LLM 自己在 `reason`/`suggestion` 里承认"此处排版错乱""跨行错位""乱码"
       （`config.LAYOUT_ARTIFACT_KEYWORDS`，关键词是从真实输出里摘的原话）——它是在说
       "我也没看懂这段被打乱的版面"，不是在报语言错误。
    2. 建议的替换内容和原文的唯一差异是空格（`"2 0 2 5年9⽉1 1⽇"` → `"2025年9月11日"`），
       空格来自PDF跨行拼接/装饰性字间距等排版原因。

    先套 `_normalize_lookalike` 再去空格比较，不能直接拿原始文本去空格：真实案例里空格差异
    经常和部首替代字同时出现在同一条issue里（`⽉` 是部首替代字、其余是空格差异），只看原始
    文本会因为部首字符不相等而漏判。
    """
    combined = f"{raw.reason}{raw.suggestion}"
    if any(k in combined for k in config.LAYOUT_ARTIFACT_KEYWORDS):
        return True
    pair = _extract_replacement_pair(raw)
    if pair is None:
        return False
    old, new = pair
    has_real_space_diff = _strip_whitespace(old) != old.strip() or _strip_whitespace(new) != new.strip()
    return has_real_space_diff and _strip_whitespace(_normalize_lookalike(old)) == _strip_whitespace(_normalize_lookalike(new))


def _is_self_declared_non_issue(raw: RawIssue) -> bool:
    """LLM 在 suggestion/reason 里自己说明"这条按规避规则不该报告为问题"。

    提示词"补充规则三：历史反馈规避"要求命中规避规则的内容直接不要输出，但 LLM 常见的
    失败模式是照样输出一条issue、把"我为什么不该报它"写成建议正文（真实案例：
    suggestion='"预测 点检"因换行被拆分，根据历史反馈规避规则第三条，此类因换行导致的
    词语拆分不应报告为问题。'）。这类条目的建议文本与原文相差悬殊，既有的零改动/纯空格
    判据全都接不住，会一路落到 `_rule_default` 判成确定性错误。关键词见
    `config.NON_ISSUE_SELF_DECLARATION_KEYWORDS`。
    """
    combined = f"{raw.reason or ''}{raw.suggestion or ''}"
    return any(k in combined for k in config.NON_ISSUE_SELF_DECLARATION_KEYWORDS)


def _filter_visually_no_op(raw_issues: list[RawIssue]) -> tuple[list[RawIssue], int, int, int, int]:
    """丢弃四类没有核实价值的假问题，在 classify_issue 之前对 RawIssue 直接过滤、整条
    丢弃，不进入最终结果——与规则C/D/G/H/I"降级为存疑待核实、保留可审计性"的一般惯例
    不同：这几类已经确认没有人工核实的价值，用户明确要求直接从结果里丢弃，不走归层。

    1. 视觉/语义零改动（`_is_visually_no_op_suggestion`）。
    2. original_text本身含解析产生的控制字符乱码（`_has_stray_control_chars`）。
    3. 版式错乱措辞 / 建议与原文只差空格（`_is_layout_or_space_artifact`）。
    4. LLM自陈"按规避规则本来就不该报"（`_is_self_declared_non_issue`）。

    第3类**是丢弃而不是降级，这是用户的明确要求**：降级会留下一句"建议对照原文核实后再
    处理"，那本身就是噪声——用户看到的仍是一条要处理的问题，而它百分之百不是原文的错。
    **代价要知道**：版式错乱那一支是关键词匹配，
    比另外两类宽，真错误若被LLM描述成"跨行/错位"会一并丢掉；丢弃数进 warnings，在界面的
    "提示信息"面板可见，不是悄无声息地消失。

    返回 (保留的issues, 零改动丢弃数, 控制字符乱码丢弃数, 解析伪影丢弃数, 自陈非问题丢弃数)。
    """
    kept = []
    no_op_dropped = 0
    control_char_dropped = 0
    artifact_dropped = 0
    self_declared_dropped = 0
    for raw in raw_issues:
        if _is_self_declared_non_issue(raw):
            self_declared_dropped += 1
            logger.info("丢弃LLM自陈不该报告的问题: 原文=%s 建议=%s", raw.original_text, raw.suggestion)
            continue
        if _has_stray_control_chars(raw.original_text or ""):
            control_char_dropped += 1
            logger.info("丢弃含解析乱码控制字符的问题: 原文=%r", raw.original_text)
            continue
        if _is_visually_no_op_suggestion(raw):
            no_op_dropped += 1
            logger.info("丢弃视觉无实质改动的建议: 原文=%s 建议=%s", raw.original_text, raw.suggestion)
            continue
        if _is_layout_or_space_artifact(raw):
            artifact_dropped += 1
            logger.info("丢弃版式错乱/纯空格差异的解析伪影: 原文=%s 建议=%s", raw.original_text, raw.suggestion)
            continue
        kept.append(raw)
    return kept, no_op_dropped, control_char_dropped, artifact_dropped, self_declared_dropped
