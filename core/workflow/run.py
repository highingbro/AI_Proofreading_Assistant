"""标准校对全链路编排。"""

from __future__ import annotations

import config
from core.chunker import chunk_document
from core.classifier import ClassifiedResult, classify_issues
from core.comparer import compare_documents
from core.feedback_rules import load_rejection_rules_text
from core.parser import ParsedDocument, parse_document
from core.proofreader import proofread_document


def run_standard_proofread(
    file_path,
    progress_callback=None,
    mode: str = config.PROOFREAD_MODE_DEEP,
    db_path=None,
) -> tuple[ClassifiedResult, ParsedDocument]:
    """标准校对全链路：解析→分块→逐块LLM校对→结果分层。纯编排，不写库。

    返回 (ClassifiedResult, ParsedDocument) 而不是单个 ClassifiedResult——parsed 要传给
    persist_result 用于计算 context_snippet，ParsedDocument 只在本次调用链上存在，过后
    无法从数据库反查，必须在这里一并交出去。

    mode 透传给 proofread_document 控制校对规则子集（精简/深度），也透传给 classify_issues
    控制精简模式下语法结构问题统一按风格可选归层。

    db_path 只用于把 load_rejection_rules_text(db_path) 需要的库路径从调用方传进来，
    本函数本身不写库——落库是 persist_result 单独负责的。load_rejection_rules_text
    只读库、不调用LLM：历史反馈的语义总结在"拒绝时"（app.py 调用
    core.feedback_rules.regenerate_rejection_rules）就已完成并落到 feedback_rules 表，
    这里只是读出已总结好的规则文本注入校对提示词，让LLM在生成建议这一步就规避曾被拒绝
    的问题模式（详见 core/feedback_rules.py 模块docstring）；表为空时返回空字符串，
    等价于没有这项增强。
    """
    parsed = parse_document(file_path)
    chunked = chunk_document(parsed)
    rejection_rules_text = load_rejection_rules_text(db_path=db_path)
    proofread_result = proofread_document(
        chunked,
        progress_callback=progress_callback,
        mode=mode,
        rejection_rules_text=rejection_rules_text,
    )
    return classify_issues(proofread_result, parsed, chunked, mode=mode), parsed


def run_document_comparison(original_path, formatted_path) -> tuple[list[dict], ParsedDocument]:
    """原稿比对全链路：双份解析→比对（core.comparer.compare_documents）。纯编排，不写库。

    返回 (diffs, formatted)——formatted(ParsedDocument) 要传给 persist_comparison_result
    用于计算 context_snippet，原因与 run_standard_proofread 返回 parsed 一致：追问的
    上下文窗口计算依赖 ParsedDocument.blocks，只在本次调用链上存在，必须一并交出去。
    """
    original = parse_document(original_path)
    formatted = parse_document(formatted_path)
    diffs = compare_documents(original, formatted)
    return diffs, formatted
