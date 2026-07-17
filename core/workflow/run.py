"""标准校对全链路编排（阶段6实现）。"""

from __future__ import annotations

import config
from core.chunker import chunk_document
from core.classifier import ClassifiedResult, classify_issues
from core.comparer import compare_documents
from core.feedback import load_learned_feedback
from core.glossary import build_glossary, format_glossary_for_prompt
from core.parser import ParsedDocument, parse_document
from core.proofreader import proofread_document


def run_standard_proofread(
    file_path,
    progress_callback=None,
    mode: str = config.PROOFREAD_MODE_DEEP,
    db_path=None,
) -> tuple[ClassifiedResult, ParsedDocument]:
    """标准校对全链路：解析→分块→构建全局术语表→逐块LLM校对→结果分层。纯编排，不写库。

    返回 (ClassifiedResult, ParsedDocument) 而非阶段6原先的单个 ClassifiedResult——
    parsed 要传给 persist_result 用于计算 context_snippet（阶段7新增），ParsedDocument
    只在本次调用链上存在，过后无法从数据库反查，必须在这里一并交出去。

    mode 透传给 proofread_document 控制校对规则子集（精简/深度），也透传给 classify_issues
    控制精简模式下语法结构问题统一按风格可选归层，默认深度模式，与该参数新增前的行为一致。

    build_glossary(parsed) 在分块之后、逐块校对之前跑一次（详见 core/glossary.py 模块
    docstring）：统计全文档候选词条+一次LLM调用做语义分类，产出全局术语表，注入每个
    chunk的校对提示词，解决chunk间互不可见导致的跨块一致性误判。build_glossary 内部
    已自带异常兜底（失败返回空列表），这里不需要额外try/except——glossary_text 为空
    字符串时，proofread_document 行为与没有这项增强前完全一致。

    db_path（阶段12新增）此前不存在——本函数一直不写库，落库是 persist_result 单独
    负责的。新增它只是为了把 load_learned_feedback(db_path) 需要的库路径从调用方传
    进来，本函数本身依然不写库。load_learned_feedback 读取历史人工反馈（拒绝issue时
    core.feedback.record_rejection 写入），驱动 classify_issues 里的规则J（人工反馈
    学习自动降级，详见 core/classifier/CLAUDE.md）；返回空列表时该规则不生效，行为
    与这项增强上线前完全一致。
    """
    parsed = parse_document(file_path)
    chunked = chunk_document(parsed)
    glossary_text = format_glossary_for_prompt(build_glossary(parsed))
    proofread_result = proofread_document(
        chunked, progress_callback=progress_callback, mode=mode, glossary_text=glossary_text
    )
    learned_feedback = load_learned_feedback(db_path=db_path)
    return (
        classify_issues(proofread_result, parsed, chunked, mode=mode, learned_feedback=learned_feedback),
        parsed,
    )


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
