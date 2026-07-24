"""反馈规则语义总结（把历史拒绝记录变成校对提示词里的规避规则）。

同一类被人工拒绝过的问题（点击"拒绝"）不应该在后续校对里反复出现。本模块把历史拒绝
记录整批交给LLM做语义总结，只归并**真正同一类原因**（不是措辞句式相似）且达到阈值
次数的模式，产出规则文本注入校对LLM的系统提示词，让LLM在生成建议这一步就主动规避，
而不是先生成再事后改判分类结果。

不用字符串相似度（如 `difflib.SequenceMatcher.ratio()`）判断"是否同一类问题"：归一化
后"应改为『X』或『Y』"这种LLM高频措辞模板本身就会撞车，会让语义完全无关的建议被错误
归并为"同类"（真实数据验证过这个失败模式）。语义总结的失败模式更安全——判断得不准，
最坏情况是又被问了一次、正常拒绝一次，不会把两个毫不相关的问题错误归并。

`regenerate_rejection_rules()`——重新生成规则表，在 `app.py` 每次记录一条拒绝之后调用
（也可在管理页手动触发）：
1. 读取全部历史反馈，**过滤掉 `config.FEEDBACK_EXEMPT_ISSUE_TYPES`（事实类）**——这是
   项目设计铁律"涉及人名/职务/历史事实的修改建议禁止确定性结论，必须保留人工复核机会"
   的延伸，在喂给LLM之前就从源头排除，不依赖总结LLM自己判断"这条该不该排除"。
2. 剩余行数不足 `config.FEEDBACK_REJECTION_THRESHOLD` 直接返回，不发起LLM调用。
3. 调用LLM做语义总结（`prompt/feedback_rules_system.md`），要求只归并**语义上真正同类**
   （不是句式相似）且达到阈值次数的模式，输出规则文本+佐证的 `feedback_id` 列表。
4. **代码层再校验一道**：每条规则的 `matched_feedback_ids` 数量必须真的 ≥ 阈值，不足的
   丢弃——不完全信任LLM自报"这个够阈值了"，延续项目"系统层兜底校验，不能只信LLM自报"
   的设计铁律。
5. 任何一步失败（LLM调用异常、JSON解析失败）都记录warning、直接返回，**不清空已有的
   feedback_rules表**——避免一次瞬时LLM故障就把已经总结好的规则清空，这是增强项，不是
   必需环节。

`load_rejection_rules_text()`——校对主流程（`core/workflow/run.py`）调用，纯DB读取，
不调用LLM，保持"每次校对不额外增加一次LLM往返"（规则的生成时机是"拒绝时"，不是
"校对时"，两者解耦）。

## 为什么拆成"生成并落库"与"读库并格式化"两个函数

规则的生成成本（一次LLM调用）和使用频率（每次校对都要用）不匹配——历史反馈在两次
拒绝之间不会变化，没必要每次校对都重新总结一遍。`regenerate_rejection_rules` 只在
拒绝发生时调用一次，产出落库；`load_rejection_rules_text` 是每次校对都会调用的纯DB
读取+格式化，不触发LLM，保持校对主流程不因这项功能增加额外的LLM往返。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import config
from core.llm_client import LLMCallError, chat_completion
from core.proofreader.response_parser import _parse_json_array
from db.models import get_feedback, get_feedback_rules, replace_feedback_rules

__all__ = ["regenerate_rejection_rules", "load_rejection_rules_text"]

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompt" / "feedback_rules_system.md"


def regenerate_rejection_rules(db_path=None) -> None:
    """重新生成 feedback_rules 表，详见模块docstring"regenerate_rejection_rules"一节。"""
    rows = get_feedback(db_path=db_path)
    rows = [r for r in rows if r["issue_type"] not in config.FEEDBACK_EXEMPT_ISSUE_TYPES]
    if len(rows) < config.FEEDBACK_REJECTION_THRESHOLD:
        return  # 不可能凑够一条规则，不发起LLM调用

    valid_ids = {r["feedback_id"] for r in rows}
    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    user_content = json.dumps(
        [
            {
                "feedback_id": r["feedback_id"],
                "issue_type": r["issue_type"],
                "original_text": r["original_text"],
                "suggestion": r["suggestion"],
                "reason": r["reason"] or "",
            }
            for r in rows
        ],
        ensure_ascii=False,
    )

    try:
        raw_response = chat_completion(system_prompt, user_content)
    except LLMCallError as exc:
        logger.warning("反馈规则总结失败(LLM调用失败): %s", exc)
        return

    try:
        raw_items = _parse_json_array(raw_response)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("反馈规则总结失败(输出无法解析为JSON): %s", exc)
        return

    rules: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        rule_text = item.get("rule")
        matched_ids = item.get("matched_feedback_ids")
        if not isinstance(rule_text, str) or not rule_text.strip():
            continue
        if not isinstance(matched_ids, list):
            continue
        matched_ids = [i for i in matched_ids if isinstance(i, int) and i in valid_ids]
        if len(matched_ids) < config.FEEDBACK_REJECTION_THRESHOLD:
            continue  # 代码层兜底：不信任LLM自报"够阈值了"
        rules.append({"rule_text": rule_text, "matched_feedback_ids": matched_ids})

    replace_feedback_rules(rules, db_path=db_path)


def load_rejection_rules_text(db_path=None) -> str:
    """纯DB读取+格式化，不调用LLM，见模块docstring。表为空返回空字符串。"""
    rules = get_feedback_rules(db_path=db_path)
    if not rules:
        return ""
    return "\n".join(f"- {r['rule_text']}" for r in rules)
