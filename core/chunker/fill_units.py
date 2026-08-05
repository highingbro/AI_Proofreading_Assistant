"""block → 填充单元：处理"超长block切分""table区域整体跳过"两条例外规则。"""

from __future__ import annotations

import re
from dataclasses import dataclass

import config
from core.parser import ParsedBlock

# 单个 block 超过 CHUNK_SIZE_MAX 时，在句末（。！？）处切分
_SENTENCE_END = re.compile(r"(?<=[。！？])")

# 排除"含读不出的字形的那一句"时用的切分点。比 _SENTENCE_END 多收分号/省略号/换行及其后的
# 收尾引号括号——这里是要尽量缩小被排除的范围，切得越细丢得越少；而超长block切分要的是
# 语义完整的段，两者目标不同，故不共用一个正则。
_CLAUSE_END = re.compile(r'(?<=[。！？；…\n])(?![”’」』）】〕》])')


@dataclass
class _FillUnit:
    """贪心装填的最小单位。通常对应一个完整 block；超长 block 按句切分后，
    多个 _FillUnit 会共享同一个 block_index（例外情况，见 _build_fill_units）。
    """

    block_index: int
    text: str
    block_type: str
    page: int


def _build_fill_units(blocks: list[ParsedBlock], max_size: int, warnings: list[str]) -> list[_FillUnit]:
    """把 block 列表转成贪心装填用的填充单元。

    block_type=="table" 的区域整体跳过，不生成任何 _FillUnit，也就不会出现在
    任何 chunk.text 里——即不会被送去LLM校对。起因：这类区域绝大多数是说明性
    UI截图/菜单结构图，不是作者撰写的待校对正文；真实数据显示77%(72/93)的
    "确定性错误"层问题定位在table类block里，其中大量是版面检测框把无关侧边栏
    内容混入、或无边框并排列表被按坐标拉平导致的结构性伪影，根本不是原文档的
    错误。跳过这整类block比"识别后再降级"更直接：这类内容压根不该进入校对
    判断。ParsedBlock 本身不受影响（parsed.blocks 仍保留完整的table区域文本，
    供未来展示/导出等场景使用），只是分块阶段不再把它纳入送审文本。详细诊断
    数据见 core/chunker/CLAUDE.md。

    第二条同类规则：含"字形读不出"记号的**那一句**整句排除，见 _drop_unreadable_clauses。
    两条规则同样只影响送审文本、不动 ParsedBlock。
    """
    units: list[_FillUnit] = []
    for b in blocks:
        if b.block_type == "table":
            continue
        text = _drop_unreadable_clauses(b.text)
        if not text:
            continue  # 整块都是读不出字形的句子
        if len(text) <= max_size:
            # 正常大小的block，原样作为一个填充单元
            units.append(_FillUnit(b.block_index, text, b.block_type, b.page))
        else:
            # 普通超长段落：按句末切分成多个片段，多个 _FillUnit 共享同一
            # block_index，后续 block_indices 去重后仍视为同一个 block
            pieces = _split_oversized_text(text, max_size)
            warnings.append(
                f"第{b.block_index}块（{b.source_location}）长度{len(text)}"
                f"超过硬上限{max_size}字符，已按句末切分为{len(pieces)}段"
            )
            for piece in pieces:
                units.append(_FillUnit(b.block_index, piece, b.block_type, b.page))
    return units


def _drop_unreadable_clauses(text: str) -> str:
    """丢掉含 `config.NATIVE_UNREADABLE_GLYPH_MARK` 的那一句，同块其余句子照常送审。

    那个记号表示"此处原文有一个字，但它的字形无法从文件里读出"（成因与还原尝试见
    `core/parser/_glyph_repair.py`）。这些位置**没法校对**：字形读不出来，就无从判断作者
    有没有写错。送进 LLM 只会得到两类假问题——把记号本身报成错别字，或者对着缺字的句子
    猜出一条"漏字"。所以整句排除。

    **按句而不是按块排除**：实测最坏情况（完全不还原）按句丢 14.9% 的正文、按块丢 20.3%，
    更要紧的是按块会让"某个 241 字长段里只有一个字读不出"赔上整段。还原之后的真实残留很小
    （实测 98 处伪造字符还原后只剩 7 处）。

    **块内找不到任何切分点时整块丢弃**（标题、条目那类，本来就短）。
    """
    if config.NATIVE_UNREADABLE_GLYPH_MARK not in text:
        return text
    clauses = [c for c in _CLAUSE_END.split(text) if c]
    return "".join(c for c in clauses if config.NATIVE_UNREADABLE_GLYPH_MARK not in c)


def _split_oversized_text(text: str, max_size: int) -> list[str]:
    """在句末（。！？）处切分超长文本，每段尽量不超过 max_size。"""
    sentences = [s for s in _SENTENCE_END.split(text) if s]
    if not sentences:
        sentences = [text]

    pieces: list[str] = []
    current = ""  # 正在累积、尚未定长的片段缓冲区
    for s in sentences:
        if len(s) > max_size:
            # 单句本身也超长（罕见），不再强求句子完整，硬切保底
            if current:
                # 先把之前攒的片段收尾入队，再单独处理这个超长句
                pieces.append(current)
                current = ""
            # 按 max_size 定长硬切，牺牲句子完整性以保证片段不超限
            for start in range(0, len(s), max_size):
                pieces.append(s[start : start + max_size])
            continue
        if current and len(current) + len(s) > max_size:
            # 再加这一句就会超限，先把当前缓冲区收尾，这句作为下一片段的开头
            pieces.append(current)
            current = s
        else:
            # 还装得下，继续累积到当前片段
            current += s
    if current:
        # 循环结束时缓冲区里可能还剩最后一段未收尾，补上
        pieces.append(current)
    return pieces
