"""校对模块的数据结构与异常类型。"""

from __future__ import annotations

from dataclasses import dataclass, field


class LLMResponseError(Exception):
    """LLM输出无法解析为合法JSON（含重试一次后仍失败）。raw_response 存原始回复供排查。"""

    def __init__(self, message: str, raw_response: str = ""):
        super().__init__(message)
        self.raw_response = raw_response


@dataclass
class RawIssue:
    original_text: str
    issue_type: str
    category: str        # LLM自报，待 core/classifier/ 校验
    confidence: str
    suggestion: str
    reason: str
    block_index: int | None   # 定位失败为None
    page_location: str | None  # locate_block() 的结果
    chunk_index: int
    located: bool              # 是否成功定位


@dataclass
class ProofreadResult:
    issues: list[RawIssue] = field(default_factory=list)
    chunk_warnings: list[str] = field(default_factory=list)  # "第N块校对失败: <错误信息>"
