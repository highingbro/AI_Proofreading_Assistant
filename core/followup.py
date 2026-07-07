"""对话追问处理模块（阶段7实现）。

负责维护"当前问题条目 + 原文上下文"的会话状态，
将用户追问路由到对应条目并携带上下文调用 LLM。本阶段仅留函数签名。
"""


def answer_followup(issue_id: int, question: str) -> str:
    """针对指定问题条目的追问，携带其上下文调用 LLM 并返回回答。"""
    raise NotImplementedError
