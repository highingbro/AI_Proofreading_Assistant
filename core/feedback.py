"""用户反馈学习模块（阶段12实现，单文件）。

真实使用中发现：同一类被人工判定"判错了"的问题（点击"拒绝"），会在后续校对（同一
文档的其他位置、或后续上传的同类文档）里反复出现，需要重新人工拒绝一遍，助手没有
任何"记忆"。本模块记录每次拒绝、并在达到阈值后驱动 core/classifier 的一条 modifier
规则（规则J，见 core/classifier/modifier_rules.py）自动把同类问题降级为"风格可选"。

## 判定信号：original_text精确匹配 或 suggestion/reason 归一化后相似度

同 issue_type 前提下，三档信号命中任一档都计入累计次数：
1. original_text 精确重复——同一份文档的模板化文字、或反复上传的同系列文档里的样板文字。
2. suggestion（LLM给出的修改建议）归一化后用 difflib 算相似度达到阈值。
3. reason（LLM给出的判定依据说明）归一化后同样用 difflib 算相似度达到阈值。

**归一化**（`_normalize_variable_parts`）：把文本里"具体是哪个字/哪个数字"这类随场景变化
的片段替换成统一占位符再比较——引号包裹的内容（书名号外的中英文引号，通常是"具体改成
什么"）统一替换成`Q`，数字统一替换成`#`。起因：真实例子里同一种"编号后面用了全角句号"
的问题，在文档的第1~7条编号都被人工拒绝过，但第10条编号（"10.发布成绩"）换了个新的具体
数字/文字内容，原始 suggestion 整句字面相似度只有约0.48（远低于阈值），不做归一化会导致
这类"同一套改写模板、具体内容不同"的重复噪音无法被识别为同一类问题；归一化后两条 suggestion
都变成"应改为Q或Q"，相似度变成1.0，能正确识别为同一类。

**为什么现在也用 reason，且用同一套归一化**：早期版本判定信号只用 suggestion、不用 reason，
理由是"reason 措辞更随意/模板化，容易在完全无关的问题之间偶然撞相似"——但归一化本身已经
去掉了大部分"偶然撞相似"的噪音来源（具体数字/具体文字这类最容易造成误判的可变部分），
reason 和 suggestion 面临的风险不再有本质差异，用户明确要求把 reason 也纳入判定、且两者
只要有一个命中阈值就算，不再区分对待。suggestion 在实时校对流程和历史记录页两种 issue
对象上都完整可用；reason 只有实时流程当次内存里的 ClassifiedIssue 才有，历史记录页的
issue 对象没有这个属性——`count_similar_rejections` 对 reason 为空的一侧直接跳过这档信号
（不当空字符串比较），不会因此产生虚假匹配，也不会报错。

阈值从早期的0.70调低到 `config.FEEDBACK_SIMILARITY_THRESHOLD`（现0.60）——归一化已经把最容易
造成误判的可变内容剥离了，剩下的字面差异更能反映"是不是同一种改写模板"，不需要原来那么高的
门槛；真实无关内容归一化后 ratio 仍接近0，调低阈值不会带来明显的误伤（详见 core/classifier/CLAUDE.md
"规则J"一节的验证数据）。

## 分类器不直接查库

load_learned_feedback 在 core/workflow/run.py 里每次校对开始时调用一次（同 build_glossary
的调用时机模式），产出的 list[LearnedFeedback] 以参数形式一次性注入 classify_issues，
core/classifier 本身不导入本模块的任何DB读写函数——保持其"纯规则逻辑，不调用LLM/不查库"
的既有设计定位。count_similar_rejections 是纯函数，classifier 的 modifier 规则直接调用。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

import config
from db.models import add_feedback, delete_feedback, get_feedback

__all__ = [
    "LearnedFeedback",
    "record_rejection",
    "load_learned_feedback",
    "count_similar_rejections",
    "cluster_feedback",
    "forget_feedback",
]


@dataclass(frozen=True)
class LearnedFeedback:
    """分类器用的精简数据形状。reason 默认空字符串，兼容历史记录页等不带 reason 的场景。"""

    issue_type: str
    original_text: str
    suggestion: str
    reason: str = ""


_QUOTE_PATTERNS = (
    re.compile(r"“[^”]*”"),
    re.compile(r"‘[^’]*’"),
    re.compile(r'"[^"]*"'),
)
_DIGIT_PATTERN = re.compile(r"\d+")


def _normalize_variable_parts(text: str) -> str:
    """把引号包裹的具体内容、数字统一替换成占位符，只保留"改写模板"的骨架，见模块docstring。"""
    for pattern in _QUOTE_PATTERNS:
        text = pattern.sub("Q", text)
    return _DIGIT_PATTERN.sub("#", text)


def record_rejection(issue, issue_id: int, record_id: int, db_path=None) -> None:
    """issue被人工拒绝时调用，记录一条反馈快照。

    suggestion 在实时校对流程（ClassifiedIssue）和历史记录页（_row_to_issue_view 包装的
    SimpleNamespace）两种 issue 对象上都存在，不需要兜底；reason 只在实时流程的
    ClassifiedIssue 上存在，用 getattr 兜底成空字符串——这里只影响管理页的展示信息，
    不影响匹配（匹配不用 reason）。
    """
    add_feedback(
        issue_type=issue.issue_type,
        original_text=issue.original_text,
        suggestion=issue.suggestion,
        reason=getattr(issue, "reason", "") or "",
        source_issue_id=issue_id,
        source_record_id=record_id,
        db_path=db_path,
    )


def load_learned_feedback(db_path=None) -> list[LearnedFeedback]:
    """读取全部历史反馈记录，映射成分类器用的精简数据形状。"""
    rows = get_feedback(db_path=db_path)
    return [
        LearnedFeedback(
            issue_type=row["issue_type"] or "",
            original_text=row["original_text"] or "",
            suggestion=row["suggestion"] or "",
            reason=row["reason"] or "",
        )
        for row in rows
    ]


def _is_similar(
    issue_type_a: str, original_text_a: str, suggestion_a: str, reason_a: str,
    issue_type_b: str, original_text_b: str, suggestion_b: str, reason_b: str,
    threshold: float,
) -> bool:
    """判断两条issue是不是"同一类问题"，count_similar_rejections/cluster_feedback共用
    同一份定义，保证"驱动规则J自动降级"和"管理页怎么分组展示"口径完全一致，不会走偏。
    """
    if issue_type_a != issue_type_b:
        return False
    normalized_text_a = (original_text_a or "").strip()
    if normalized_text_a and (original_text_b or "").strip() == normalized_text_a:
        return True
    normalized_suggestion_a = _normalize_variable_parts(suggestion_a or "")
    if normalized_suggestion_a and suggestion_b:
        ratio = difflib.SequenceMatcher(
            None, normalized_suggestion_a, _normalize_variable_parts(suggestion_b)
        ).ratio()
        if ratio >= threshold:
            return True
    normalized_reason_a = _normalize_variable_parts(reason_a or "")
    if normalized_reason_a and reason_b:
        ratio = difflib.SequenceMatcher(
            None, normalized_reason_a, _normalize_variable_parts(reason_b)
        ).ratio()
        if ratio >= threshold:
            return True
    return False


def count_similar_rejections(
    issue_type: str,
    original_text: str,
    suggestion: str,
    reason: str,
    learned: list[LearnedFeedback],
) -> int:
    """统计 learned 里有多少条历史反馈与本条issue"同类"：同 issue_type 前提下，
    original_text 精确匹配（strip后相等）、或 suggestion/reason 归一化后相似度达到阈值
    （任一档命中即计数，不要求两档都命中），见模块docstring"判定信号"一节。
    """
    threshold = config.FEEDBACK_SIMILARITY_THRESHOLD
    return sum(
        1
        for entry in learned
        if _is_similar(
            issue_type, original_text, suggestion, reason,
            entry.issue_type, entry.original_text, entry.suggestion, entry.reason,
            threshold,
        )
    )


def cluster_feedback(rows: list) -> list[list]:
    """把历史反馈按"是不是同一类问题"聚类，判定信号与 count_similar_rejections 完全
    一致（都调 _is_similar），保证管理页展示的分组跟真正驱动自动降级的判定口径一致。

    起因：管理页早期版本直接按 issue_type 分组展示（如"错别字与拼写(19条)"），用户反馈
    这个分组具有误导性——issue_type 相同不代表是"同一类问题"，一条跟其余18条内容毫无
    关系的新拒绝，视觉上会被归进同一个已经"达到阈值"的大分组里，容易让人误以为这次拒绝
    也会被计入自动降级判定，但真正驱动降级的 count_similar_rejections 并不会这样匹配。
    按真实相似度聚类后，一条无关的新拒绝会独立成一个"1条"的簇，不会跟其它簇混在一起。

    **两阶段聚类，不是单阶段（补丁，真实数据验证后从单阶段全连接改过来）**：

    阶段1——按 (issue_type, 原文精确相等) 分组打底。原文精确相等具有传递性（A和B原文
    逐字相同、B和C原文也逐字相同，那A和C原文必然也相同，这是数学保证，不是启发式），
    不存在下面阶段2要规避的"链式误判"风险，直接分组，不受阈值影响。

    阶段2——把阶段1的组当作最小单位，用 suggestion/reason 归一化后的模糊相似度做
    complete-linkage合并：一个组要并入另一个簇，组内**每一条**都要跟目标簇里**已有的
    每一条**模糊匹配，缺一个都不合并。真实数据踩过两种坑，两阶段设计是为了同时避开：

    1. 最初版本（单阶段、并查集单链传递）：A与B相似、B与C相似就把A/C也合并，"默认1分"
       同时踩中两处阈值边缘（跟"可得X分"三条的原文相似度、跟某条日期反馈的说明相似度都
       刚好过线），把"日期空格"和"数字单位间空格"这两个本不相关的紧密簇焊成了一个8条
       大簇，链两端彼此一点都不像。
    2. 改成单阶段complete-linkage（要求新成员跟簇内每条都模糊匹配）后，上面的误合并
       消失了，但暴露了新问题：3条原文**逐字完全相同**的"2026年7月13日"反馈，只有2条
       聚在一起、第3条被拆成单独一簇——因为"默认1分"先靠模糊相似度混进了前2条组成的
       簇，成了这个簇里的"陌生成员"；第3条虽然原文和前2条逐字相同，但跟"默认1分"模糊
       不匹配，导致"必须匹配簇内每一条"这个条件被"默认1分"这个混进来的陌生成员卡住，
       原文精确相同这个最强信号反而没起到应有的强制合并作用。

    两阶段设计让"原文精确相同"在阶段1无条件生效（不会被任何后续加入的模糊匹配成员
    干扰），"模糊相似度"只在阶段2用于合并不同的精确组之间的关系，两种信号的定位不再
    互相踩踏。真实数据验证：3条日期正确聚成一簇，"默认1分"能否加入"可得X分"这组，
    只取决于它是否同时和这3条本身都模糊匹配，不再受制于任何后来才加入的第三方。

    参数是 db.models.get_feedback() 返回的原始行（dict-like，含 feedback_id/created_at
    等展示/删除按钮需要的字段），不是 LearnedFeedback——保持和管理页调用方直接对接，
    不需要额外转换。按簇大小降序返回。O(N^2) 两两比较：反馈量级对单人本地工具预期
    长期停留在几十到小几百条，这个复杂度足够快，不需要更复杂的算法。
    """
    threshold = config.FEEDBACK_SIMILARITY_THRESHOLD

    # 阶段1：原文精确相等分组（传递性有数学保证，不需要任何相似度判断）。
    exact_groups: dict[tuple[str, str], list] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = (row["issue_type"], (row["original_text"] or "").strip())
        if key not in exact_groups:
            exact_groups[key] = []
            order.append(key)
        exact_groups[key].append(row)
    units = [exact_groups[k] for k in order]

    # 阶段2：以阶段1的组为最小单位，用模糊相似度做complete-linkage合并——组与组
    # 之间要合并，要求两组所有行两两都模糊匹配，不满足就不合并，避免链式效应。
    clusters: list[list] = []
    for unit in units:
        target = None
        for cluster in clusters:
            if all(
                _is_similar(
                    a["issue_type"], a["original_text"], a["suggestion"], a["reason"],
                    b["issue_type"], b["original_text"], b["suggestion"], b["reason"],
                    threshold,
                )
                for a in unit
                for b in cluster
            ):
                target = cluster
                break
        if target is not None:
            target.extend(unit)
        else:
            clusters.append(list(unit))

    return sorted(clusters, key=len, reverse=True)


def forget_feedback(feedback_id: int, db_path=None) -> None:
    """管理页"撤销此条反馈"按钮调用，删除一条历史反馈记录。"""
    delete_feedback(feedback_id, db_path=db_path)
