"""原稿比对模块。

负责原稿与排版稿的双份解析、差异检测与分类（排版调整 / 实质性内容改动）。

核心难点是同一份内容用两种格式给过来时，**两边的块划分
根本不是同一套东西**：Word 走 `core/parser/docx_parser.py`，一个 Word 段落一个块；
PDF 走 A 类通道，段落边界写在块文本内部的 `\n` 里，`_paragraphs.py` 还会把被栏宽切开
的续写块并回上一块——实测同一篇文章 Word 侧 29 块、PDF 侧 2 块。任何"按块列表对齐"
的做法在这里都会把整篇文章判成"原稿全删 + 排版稿全增"。

所以比对不看块边界，把两份文档各自拉平成**一条归一化文本流**再切句子比：

    normalize（去排版噪音）→ 拼成全文档文本流（记下每段起始偏移属于哪个块）
    → 按句末标点切句 → difflib 逐句 diff → 组装成差异条目列表

块只在两个地方用到：`block_type == 'table'` 的块整块不参与比对（PDF 侧表格是按坐标
拉平的文本，而 `docx_parser.py` 只读 `document.paragraphs`、根本不读 Word 表格，两边
天然对不上），以及差异条目要靠偏移反查回块来填页码/定位。**标题、图注这些块类型一律
参与比对**：Word 侧的"标题"取决于作者有没有套用标题样式，PDF 侧取决于版面模型认不认，
两边判定标准不同，按块类型筛就会让同一行字在一边送比、另一边不送比，凭空造出差异。

compare_documents 接收 ParsedDocument 而不是文件路径——与 core/classifier 的
classify_issues 一致，路径解析是编排层 core/workflow 的职责，这样才能用手工构造的
ParsedDocument/ParsedBlock 直接做单元测试，不用每个测试都真的解析PDF/Word。
"""

from __future__ import annotations

import bisect
import difflib
import re

import config
from core.parser import ParsedBlock, ParsedDocument


_HYPHEN_LINE_BREAK_RE = re.compile(r"([a-zA-Z])-\n([a-zA-Z])")
_INNER_WHITESPACE_RE = re.compile(r"(?<=\S)\s+(?=\S)")


def _is_word_char(char: str) -> bool:
    """拉丁字母或阿拉伯数字——词与词之间要靠空格分开的那类字符。"""
    return char.isascii() and char.isalnum()


def _collapse_whitespace(match: re.Match) -> str:
    """只看空白紧邻的前后两个字符决定留不留空格。

    不能看"空白前面整个非空白串是不是ASCII"——`1997 年通车` 里前串正好是纯数字
    `1997`，那个空格就会被保留，而 Word 侧写的是 `1997年`，凭空多出一处差异。
    这个空格来自PDF字距微调（见 core/parser/_glyphs.py），中文侧本来就不该留。
    """
    text = match.string
    if _is_word_char(text[match.start() - 1]) and _is_word_char(text[match.end()]):
        return " "
    return ""


def normalize(text: str) -> str:
    """抹掉排版重排产生的噪音：换行位置、缩进、西文跨行连字符、中文侧的假空格。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _HYPHEN_LINE_BREAK_RE.sub(r"\1\2", text)
    return _INNER_WHITESPACE_RE.sub(_collapse_whitespace, text.strip())


class _Stream:
    """一份文档拉平后的归一化文本流，附带"哪个偏移属于哪个块"的反查表。"""

    def __init__(self, text: str, starts: list[int], blocks: list[ParsedBlock]):
        self.text = text
        self._starts = starts
        self._blocks = blocks

    def block_at(self, offset: int) -> ParsedBlock | None:
        """反查偏移落在哪个块里——差异条目的页码/定位全靠它。"""
        if not self._starts:
            return None
        return self._blocks[max(0, bisect.bisect_right(self._starts, offset) - 1)]


def build_stream(document: ParsedDocument) -> _Stream:
    """把文档拉平成一条归一化文本流。

    块文本内部的 `\n` 在这里就地拆开再归一化，逐段无缝拼接——PDF 侧一个块含多段
    （块内 `\n`）和一段被页边界劈成两块，拼完都还原成同一条连续文本，与 Word 侧
    逐段拼出来的那条完全一致。
    """
    parts: list[str] = []
    starts: list[int] = []
    blocks: list[ParsedBlock] = []
    position = 0
    for block in document.blocks:
        if block.block_type in config.COMPARE_SKIP_BLOCK_TYPES:
            continue
        # 西文跨行连字符要在拆 `\n` 之前接回去——判据两侧隔着的正是那个换行符，
        # 拆完再补就看不出 `recon-` 和 `struction` 本来是一个词
        text = block.text.replace("\r\n", "\n").replace("\r", "\n")
        for piece in _HYPHEN_LINE_BREAK_RE.sub(r"\1\2", text).split("\n"):
            normalized = normalize(piece)
            if not normalized:
                continue
            parts.append(normalized)
            starts.append(position)
            blocks.append(block)
            position += len(normalized)
    return _Stream("".join(parts), starts, blocks)


def split_sentences(text: str) -> tuple[list[str], list[int]]:
    """按句末标点切分整条文本流，返回（句子列表，各句起始偏移）。

    标点保留在前一句末尾（零宽断言，不消耗字符）。切分在**文本流层面**做而不是逐段做：
    段落边界两边格式不一致，拿它当切分点等于把不一致带进比对单元。
    """
    pattern = f"(?<=[{config.COMPARE_SENTENCE_SPLIT_PUNCTUATION}])"
    sentences = [s for s in re.split(pattern, text) if s]
    offsets: list[int] = []
    position = 0
    for sentence in sentences:
        offsets.append(position)
        position += len(sentence)
    return sentences, offsets


def _snap_back(text: str, index: int) -> int:
    """把区间起点往前挪到最近一个分句标点之后（没有就挪到开头）。"""
    for i in range(index - 1, -1, -1):
        if text[i] in config.COMPARE_CLAUSE_BOUNDARY_PUNCTUATION:
            return i + 1
    return 0


def _snap_forward(text: str, index: int) -> int:
    """把区间终点往后挪到最近一个分句标点之后（没有就挪到结尾），标点含在区间内。"""
    for i in range(index, len(text)):
        if text[i] in config.COMPARE_CLAUSE_BOUNDARY_PUNCTUATION:
            return i + 1
    return len(text)


def _narrow_replace_span(orig_text: str, fmt_text: str) -> tuple[str, str]:
    """把 replace 区间收窄到真正变化的那一小段。

    切句只认句末标点，标题、图注这类不带句号的内容会和后一句粘成一个很长的比对单元
    （段落边界两边格式不一致、不能当切分点，见 split_sentences）。这里在**已经确认有
    差异**的区间内部再收一次：削掉两端完全相同的部分，再把边界外扩到最近的分句标点，
    让编辑看到"变了的那一句"而不是一大段。纯展示层收窄，不改变报不报这处差异。
    """
    limit = min(len(orig_text), len(fmt_text))
    prefix = 0
    while prefix < limit and orig_text[prefix] == fmt_text[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and orig_text[-1 - suffix] == fmt_text[-1 - suffix]:
        suffix += 1

    start = _snap_back(orig_text, prefix)  # 前缀两边相同，起点下标在两侧通用
    return (
        orig_text[start:_snap_forward(orig_text, len(orig_text) - suffix)],
        fmt_text[start:_snap_forward(fmt_text, len(fmt_text) - suffix)],
    )


_KIND_LABELS = {"insert": "新增内容", "delete": "删除内容", "replace": "文字替换"}


def _make_entry(kind: str, original_text: str, formatted_text: str, orig_block, fmt_block) -> dict:
    if kind == "delete" or fmt_block is None:
        # 排版稿里没有对应文字，只能报原稿位置；标"(原稿)"以免被当成排版稿页码
        page_location = f"(原稿){orig_block.source_location}" if orig_block else ""
        block_index = None
    else:
        page_location = fmt_block.source_location
        block_index = fmt_block.block_index

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
        "layer": config.LAYER_CONFIRMED,
        "suggestion": suggestion,
    }


def compare_documents(original: ParsedDocument, formatted: ParsedDocument) -> list[dict]:
    """比对原稿与排版稿，返回差异条目列表——排版噪音已被 normalize 与文本流拼接抹平，
    返回结果里只剩实质性内容改动。

    层级恒为 config.LAYER_CONFIRMED（错误类），走四层分类里的同一条通道：比对结果和
    标准校对结果落在同一张 issues 表里，而历史记录页是按 config.LAYERS 四层分组渲染
    问题卡的，另立一套取值的结果点进那条记录一张卡都渲染不出来（列表里却还显示着
    "总数N"，看上去像数据丢了）。归到"错误类"而不是别的层，是因为这里的差异不是LLM的
    判断而是逐字比出来的客观事实
    ——排版稿和原稿不一致这件事本身是确定的（**改得对不对**要人工定夺，那是采纳/拒绝
    要回答的问题，不是分层要回答的）。
    """
    orig_stream = build_stream(original)
    fmt_stream = build_stream(formatted)
    orig_sentences, orig_offsets = split_sentences(orig_stream.text)
    fmt_sentences, fmt_offsets = split_sentences(fmt_stream.text)

    matcher = difflib.SequenceMatcher(a=orig_sentences, b=fmt_sentences, autojunk=False)
    results = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        orig_block = orig_stream.block_at(orig_offsets[i1]) if i1 < len(orig_offsets) else None
        fmt_block = fmt_stream.block_at(fmt_offsets[j1]) if j1 < len(fmt_offsets) else None
        original_text = "".join(orig_sentences[i1:i2])
        formatted_text = "".join(fmt_sentences[j1:j2])
        if tag == "replace":
            original_text, formatted_text = _narrow_replace_span(original_text, formatted_text)
        results.append(
            _make_entry(
                tag,
                original_text=original_text,
                formatted_text=formatted_text,
                orig_block=orig_block,
                fmt_block=fmt_block,
            )
        )
    return results
