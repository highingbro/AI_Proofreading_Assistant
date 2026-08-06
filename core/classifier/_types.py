"""结果分层模块的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ClassifiedIssue:
    # ---- 继承自 RawIssue ----
    original_text: str
    issue_type: str
    suggestion: str          # 可能被本模块改写
    reason: str
    block_index: int | None
    page_location: str | None
    chunk_index: int
    located: bool
    # ---- 本模块产出 ----
    layer: str                # config.LAYER_* 四层之一
    priority: str              # '高'/'中'/'低'/'可选'
    layer_notes: list[str]     # 归层依据记录
    # ---- 供后续阶段使用 ----
    llm_category: str          # LLM原始自报，追问溯源用
    llm_confidence: str
    original_suggestion: str   # 改写前的LLM原始建议


@dataclass
class ClassifiedResult:
    issues: list[ClassifiedIssue] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class _ClassificationState:
    """修饰规则链路上传递的可变状态，替代原来四个独立变量手工穿参。"""
    layer: str
    priority: str
    suggestion: str
    notes: list[str]
