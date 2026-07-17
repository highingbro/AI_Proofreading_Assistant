"""全局术语表构建（补丁：解决chunk间互不可见导致的跨块一致性误判）。

core/proofreader/ 对每个chunk独立并发调用LLM校对，chunk之间除了相邻边界的重叠文本
外完全互不可见（见 core/proofreader/CLAUDE.md"并发执行设计决策"）。这导致同一人名/
机构名/专有名词在文档不同位置出现不同写法（如"张三"/"张叁"）时，因为两处分别落在
不同chunk，没有任何一次LLM调用能同时看到两边、意识到这是同一实体的不一致写法——
这类问题完全漏检，现有分层规则（core/classifier/）也无法弥补，因为分层只能在LLM
已经报告的问题上做后处理，报告不出来的问题无从归层。

解法：校对前先跑一次轻量预处理——本模块 build_glossary()：
1. 规则统计（_extract_candidate_terms，零API成本、零新依赖）：逐block内部滑窗生成
   n-gram，按频次筛出候选词条。不引入NER/分词等新依赖，延续 config.py 里
   FACTUAL_TITLE_KEYWORDS 注释表达过的"宁可漏判不复杂化"取向。
2. 单次LLM调用：把候选词条列表（不是全文）交给LLM做语义分类+同名异写检测，产出
   GlossaryEntry 列表。

范围收窄：只产出"检测到多种写法"的词条对跨块不一致问题有直接价值；"某术语只出现一次、
后文chunk因缺乏上下文误判为生造词"这类问题不在本模块处理范围内（需要注入全量高频词表，
prompt体积和噪音都会显著上升，价值也不如前者确定）。

失败处理：build_glossary() 是校对流程的增强项，不是必需环节，任何一步失败（候选为空、
LLM调用异常、输出无法解析）都返回空列表并记录warning，绝不抛异常、不阻断主流程——调用方
（core/workflow/run.py）据此推出 glossary_text=""，注入到系统提示词后等价于没有这份
参照，校对照常进行。

产出的 GlossaryEntry 列表经 format_glossary_for_prompt() 格式化成纯文本字符串（不是
再传递 GlossaryEntry 对象本身）注入 core/proofreader/ 的系统提示词——特意用字符串
而不是数据结构做模块间的传递接口，是为了不让 core/proofreader/ 反向依赖本模块（本模块
需要 import core.proofreader.response_parser 复用JSON解析逻辑，若 core/proofreader/
再反向 import 本模块会构成循环导入）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import config
from core.llm_client import LLMCallError, chat_completion
from core.parser import ParsedDocument
from core.proofreader.response_parser import _parse_json_array

__all__ = ["GlossaryEntry", "build_glossary", "format_glossary_for_prompt"]

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompt" / "glossary_system.md"

_HAN_RE = re.compile(r"^[一-鿿]+$")

_VALID_CATEGORIES = {"人名", "机构名", "专有术语"}


@dataclass
class GlossaryEntry:
    """全局术语表一条词条。variants 为空表示未检测到其他写法（仍可能是合法专名）。"""

    canonical: str
    category: str
    variants: list[str] = field(default_factory=list)


def _extract_candidate_terms(parsed: ParsedDocument) -> list[str]:
    """规则统计候选词条：逐block内部滑窗生成纯汉字n-gram，按频次筛选+截断。

    不跨block拼接n-gram（避免拼出跨越两段无关文字的无意义片段）；跳过
    block_type=="table"（这类区域本就不送审，见 core/chunker/CLAUDE.md）。
    """
    counts: dict[str, int] = {}
    for block in parsed.blocks:
        if block.block_type == "table":
            continue
        text = block.text
        length = len(text)
        for n in range(config.GLOSSARY_NGRAM_MIN_LEN, config.GLOSSARY_NGRAM_MAX_LEN + 1):
            if n > length:
                break
            for i in range(length - n + 1):
                gram = text[i : i + n]
                if not _HAN_RE.match(gram):
                    continue
                counts[gram] = counts.get(gram, 0) + 1

    frequent = [term for term, freq in counts.items() if freq >= config.GLOSSARY_MIN_FREQUENCY]

    # 子串去重：按长度从长到短处理，若某候选是已保留的更长候选的子串且频次相同
    # （即该候选的所有出现都嵌在那个更长候选里，本身没有独立出现），丢弃短的。
    frequent.sort(key=len, reverse=True)
    kept: list[str] = []
    for term in frequent:
        freq = counts[term]
        if any(term in longer for longer in kept if counts[longer] == freq):
            continue
        kept.append(term)

    kept.sort(key=lambda t: counts[t], reverse=True)
    return kept[: config.GLOSSARY_CANDIDATE_TOP_K]


def format_glossary_for_prompt(entries: list[GlossaryEntry]) -> str:
    """把 GlossaryEntry 列表格式化成注入系统提示词的纯文本；entries 为空返回空字符串。"""
    if not entries:
        return ""
    lines = []
    for entry in entries:
        if entry.variants:
            lines.append(
                f"- 【{entry.category}】权威写法：{entry.canonical}；"
                f"文中出现过的其他写法（疑似同一实体的不一致写法）：{'、'.join(entry.variants)}"
            )
        else:
            lines.append(f"- 【{entry.category}】{entry.canonical}")
    return "\n".join(lines)


def build_glossary(parsed: ParsedDocument) -> list[GlossaryEntry]:
    """构建全局术语表。任何一步失败都返回空列表，不抛异常（详见模块docstring"失败处理"）。"""
    candidates = _extract_candidate_terms(parsed)
    if not candidates:
        return []

    system_prompt = _PROMPT_PATH.read_text(encoding="utf-8")
    user_content = "\n".join(candidates)

    try:
        raw_response = chat_completion(system_prompt, user_content)
    except LLMCallError as exc:
        logger.warning("全局术语表构建失败(LLM调用失败): %s", exc)
        return []

    try:
        raw_items = _parse_json_array(raw_response)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("全局术语表构建失败(输出无法解析为JSON): %s", exc)
        return []

    entries: list[GlossaryEntry] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        canonical = item.get("canonical")
        category = item.get("category")
        if not isinstance(canonical, str) or not canonical.strip():
            continue
        if category not in _VALID_CATEGORIES:
            continue
        variants = item.get("variants")
        if not isinstance(variants, list):
            variants = []
        variants = [v for v in variants if isinstance(v, str) and v.strip()]
        entries.append(GlossaryEntry(canonical=canonical, category=category, variants=variants))

    return entries
