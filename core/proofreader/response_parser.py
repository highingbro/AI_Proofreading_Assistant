"""LLM输出解析与容错：剥离markdown围栏、JSON数组解析、逐条字段/枚举值校验。"""

from __future__ import annotations

import json
import re

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

_VALID_CATEGORIES = {"normal", "quotation", "factual", "style"}
_VALID_CONFIDENCES = {"high", "medium", "low"}
_REQUIRED_FIELDS = ("original_text", "issue_type", "category", "confidence", "suggestion", "reason")


def _strip_code_fence(text: str) -> str:
    match = _CODE_FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def _parse_json_array(raw_text: str) -> list[dict]:
    """strict=False：LLM常在reason/suggestion等字符串字段里直接输出裸换行/制表符等控制
    字符而不转义成\\n/\\t，标准json.loads(strict=True)会报"Invalid control character"，
    但这类内容本身是合法的多行文本，不是LLM输出格式错误，不该被当成需要重试的坏JSON。"""
    stripped = _strip_code_fence(raw_text)
    data = json.loads(stripped, strict=False)
    if not isinstance(data, list):
        raise ValueError("LLM输出不是JSON数组")
    return data


def _validate_item(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    for key in _REQUIRED_FIELDS:
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            return False
    if item["category"] not in _VALID_CATEGORIES:
        return False
    if item["confidence"] not in _VALID_CONFIDENCES:
        return False
    return True
