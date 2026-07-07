"""结果分层模块（阶段5实现）。

负责对 LLM 返回的问题做归层校验：引文类强制保护、
事实类置信度不足时降级为"存疑待核实"、风格建议归为"风格可选"。
本阶段仅留函数签名。
"""


def classify_issue(raw_issue: dict) -> dict:
    """对单条 LLM 原始问题做归层校验，返回带分层标注的问题字典。"""
    raise NotImplementedError
