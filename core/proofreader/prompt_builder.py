"""系统提示词组装：模板 + 校对规则原文，深度/精简两种模式。"""

from __future__ import annotations

import re
from pathlib import Path

import config

_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompt"
_SYSTEM_TEMPLATE_PATH = _PROMPT_DIR / "proofread_system.md"
_RULES_PATH = _PROMPT_DIR / "proofread_rules.md"

_RULE_HEADING_RE = re.compile(r"^\*\*(\d+)\.", re.MULTILINE)


def _split_rules_by_number(rules_text: str) -> dict[int, str]:
    """把 proofread_rules.md 全文按 `**N. 标题**` 顶格加粗行切分成 {规则序号: 区块原文}。

    区块范围从该加粗行起，到下一个规则/分组标题行之前（不含分组"## 一/二/三"标题本身）。
    """
    headings = list(_RULE_HEADING_RE.finditer(rules_text))
    blocks: dict[int, str] = {}
    for idx, match in enumerate(headings):
        number = int(match.group(1))
        start = match.start()
        end = headings[idx + 1].start() if idx + 1 < len(headings) else len(rules_text)
        blocks[number] = rules_text[start:end].strip()
    return blocks


def _build_system_prompt(mode: str = config.PROOFREAD_MODE_DEEP) -> str:
    """读取 proofread_system.md 模板 + proofread_rules.md 全文，做占位替换后返回完整system prompt。

    深度模式：整份规则原文注入，与阶段4原始行为完全一致。精简模式：只注入
    config.SIMPLIFIED_RULE_NUMBERS 对应的规则区块（丢弃"## 一/二/三"分组标题，
    按数字顺序拼接）。
    """
    template = _SYSTEM_TEMPLATE_PATH.read_text(encoding="utf-8")
    rules = _RULES_PATH.read_text(encoding="utf-8")
    if mode == config.PROOFREAD_MODE_SIMPLIFIED:
        blocks = _split_rules_by_number(rules)
        rules = "\n\n".join(blocks[n] for n in config.SIMPLIFIED_RULE_NUMBERS if n in blocks)
    return (
        template.replace("{{PROOFREAD_RULES}}", rules)
        .replace("{{OVERLAP_MARK}}", config.CHUNK_OVERLAP_MARK)
        .replace("{{BODY_MARK}}", config.CHUNK_BODY_MARK)
    )
