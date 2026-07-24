"""原稿比对模块。

负责原稿与排版稿的双份解析、段落对齐、差异检测与分类
（排版调整 / 实质性内容改动）。

核心难点不是"发现差异后怎么办"，而是排版重排本身会产生大量"看起来不同但内容没变"
的噪音（换行位置变了、英文单词跨行加了连字符、空格/缩进不同），必须先把这类噪音
过滤掉，剩下的才是真正需要人工确认的实质性内容改动。整体流程：

    normalize（去排版噪音）→ align_paragraphs（段落对齐）
    → 归一化后仍不同的段落，用 sentence_level_diff 找出具体差异句子
    → compare_documents 组装成差异条目列表

compare_documents 接收 ParsedDocument 而不是文件路径——与 core/classifier 的
classify_issues 一致，路径解析是编排层 core/workflow 的职责，这样才能用手工构造的
ParsedDocument/ParsedBlock 直接做单元测试，不用每个测试都真的解析PDF/Word。
"""

from __future__ import annotations

import difflib
import re

import config
from core.parser import ParsedDocument


def normalize(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"([a-zA-Z])-\n([a-zA-Z])", r"\1\2", text)
    text = re.sub(r"([^\s]+)([\s]+)", replacement, text).strip()
    return text


def replacement(match):
    match_front = match.group(1)

    if match_front.isascii():
        return match_front + " "
    else:
        return match_front


def align_paragraphs(orig_text, fmt_text):
    """对齐原稿与排版稿的段落，返回 [(原稿下标|None, 排版稿下标|None), ...]。"""
    matcher = difflib.SequenceMatcher(a=orig_text, b=fmt_text, autojunk=False)
    pairs = []
    for (tag, a1, a2, b1, b2) in matcher.get_opcodes():
        if tag == "equal":
            pairs += [(a, b) for a, b in zip(range(a1, a2), range(b1, b2))]
        elif tag == "replace":
            pairs += pair_replace_block(orig_text, fmt_text, a1, a2, b1, b2)
        elif tag == "delete":
            pairs += [(a, None) for a in range(a1, a2)]
        elif tag == "insert":
            pairs += [(None, b) for b in range(b1, b2)]
    return pairs


def pair_replace_block(orig_text, fmt_text, a1, a2, b1, b2):
    """在 [a1,a2) x [b1,b2) 这个 replace 区间内，逐对比较相似度，贪心挑出最佳匹配。

    不能只算"整个区间拼接后的一个总相似度"——两大段拼接文本可能因为共享大量重复
    措辞而整体相似度不低，掩盖掉"其中某一对具体段落其实完全不像"的事实。必须对
    区间内每一对候选 (i, j) 单独打分，按相似度从高到低贪心地依次配对（配过的 i、j
    不能再被其他候选占用），没配上的原稿下标标删除，没配上的排版稿下标标插入。
    """
    orig_indices = list(range(a1, a2))
    fmt_indices = list(range(b1, b2))

    candidates = []
    for i in orig_indices:
        for j in fmt_indices:
            ratio = difflib.SequenceMatcher(a=orig_text[i], b=fmt_text[j], autojunk=False).ratio()
            if ratio >= config.COMPARE_PARAGRAPH_MATCH_MIN_RATIO:
                candidates.append((ratio, i, j))
    candidates.sort(key=lambda c: c[0], reverse=True)

    used_orig = set()
    used_fmt = set()
    pairs = []
    for ratio, i, j in candidates:
        if i not in used_orig and j not in used_fmt:
            pairs.append((i, j))
            used_orig.add(i)
            used_fmt.add(j)

    for i in orig_indices:
        if i not in used_orig:
            pairs.append((i, None))
    for j in fmt_indices:
        if j not in used_fmt:
            pairs.append((None, j))

    # 贪心按相似度选出的配对顺序是乱的，按下标重新排回文档顺序（没有原稿下标的
    # 插入项，退而用排版稿下标近似定位）。
    pairs.sort(key=lambda p: p[0] if p[0] is not None else p[1])
    return pairs


def _split_sentences(text: str) -> list[str]:
    """按句末标点切分，标点保留在前一句末尾（零宽断言，不消耗字符）。"""
    pattern = f"(?<=[{config.COMPARE_SENTENCE_SPLIT_PUNCTUATION}])"
    return [s for s in re.split(pattern, text) if s]


def sentence_level_diff(orig_para: str, fmt_para: str) -> list[dict]:
    """对归一化后仍不同的两段文本，做句子级 diff，只返回真正不同的句子片段。"""
    orig_sentences = _split_sentences(orig_para)
    fmt_sentences = _split_sentences(fmt_para)

    matcher = difflib.SequenceMatcher(a=orig_sentences, b=fmt_sentences, autojunk=False)
    spans = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        spans.append(
            {
                "change_type": tag,
                "original_text": "".join(orig_sentences[i1:i2]),
                "formatted_text": "".join(fmt_sentences[j1:j2]),
            }
        )
    return spans


_KIND_LABELS = {"insert": "新增内容", "delete": "删除内容", "replace": "文字替换"}


def _make_entry(kind, orig_block=None, fmt_block=None, span=None) -> dict:
    if span is not None:
        original_text = span["original_text"]
        formatted_text = span["formatted_text"]
    else:
        original_text = orig_block.text if orig_block is not None else ""
        formatted_text = fmt_block.text if fmt_block is not None else ""

    if fmt_block is not None:
        page_location = fmt_block.source_location
        block_index = fmt_block.block_index
    else:
        page_location = f"(原稿){orig_block.source_location}"
        block_index = None

    if kind == "insert":
        suggestion = "排版稿新增内容，原稿无对应文字"
    elif kind == "delete":
        suggestion = f"原稿为：{original_text}，排版稿中未找到对应内容"
    else:
        suggestion = f"原稿为：{original_text}"

    return {
        "page_location": page_location,
        "block_index": block_index,
        "original_text": original_text,
        "formatted_text": formatted_text,
        "diff_type": _KIND_LABELS[kind],
        "layer": config.DIFF_LAYER_SUBSTANTIVE,
        "suggestion": suggestion,
    }


def compare_documents(original: ParsedDocument, formatted: ParsedDocument) -> list[dict]:
    """比对原稿与排版稿，返回差异条目列表——排版噪音已被 normalize+align_paragraphs
    过滤掉，返回结果里只剩实质性内容改动（层级恒为 config.DIFF_LAYER_SUBSTANTIVE）。

    只比较 block_type == 'paragraph' 的正文段落，标题/表格/图注等不参与比对。
    """
    orig_blocks = [b for b in original.blocks if b.block_type == "paragraph"]
    fmt_blocks = [b for b in formatted.blocks if b.block_type == "paragraph"]

    orig_norm = [normalize(b.text) for b in orig_blocks]
    fmt_norm = [normalize(b.text) for b in fmt_blocks]

    alignment = align_paragraphs(orig_norm, fmt_norm)

    results = []
    for orig_idx, fmt_idx in alignment:
        if orig_idx is None:
            results.append(_make_entry("insert", fmt_block=fmt_blocks[fmt_idx]))
            continue
        if fmt_idx is None:
            results.append(_make_entry("delete", orig_block=orig_blocks[orig_idx]))
            continue
        if orig_norm[orig_idx] == fmt_norm[fmt_idx]:
            continue  # 归一化后完全一致：纯排版差异，不产出条目

        for span in sentence_level_diff(orig_norm[orig_idx], fmt_norm[fmt_idx]):
            results.append(
                _make_entry(
                    "replace",
                    orig_block=orig_blocks[orig_idx],
                    fmt_block=fmt_blocks[fmt_idx],
                    span=span,
                )
            )
    return results
