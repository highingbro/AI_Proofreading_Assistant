"""阶段3验收测试：长文档分块模块。

用合成 ParsedDocument（精确控制block大小/类型，便于断言边界规则）
加真实样本冒烟测试（复用阶段2已验收的 sample_double_column.pdf）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from core.chunker import chunk_document, chunk_for_block, locate_block
from core.parser import ParsedBlock, ParsedDocument, parse_document

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def _make_text(seed: int, length: int) -> str:
    unit = f"第{seed}号测试段落内容。"
    return (unit * (length // len(unit) + 1))[:length]


def _synthetic_doc(blocks: list[ParsedBlock], total_pages: int = 1) -> ParsedDocument:
    return ParsedDocument(
        file_name="synthetic.docx",
        file_type="docx",
        total_pages=total_pages,
        blocks=blocks,
        layout_mode="single",
        text_source="native",
        warnings=[],
    )


# ---------------------------------------------------------------------------
# 完整性 / 顺序
# ---------------------------------------------------------------------------

def test_completeness_and_order():
    blocks = [
        ParsedBlock(
            page=i // 5 + 1,
            block_index=i,
            text=_make_text(i, 400),
            block_type="paragraph",
            source_location=f"第{i}段",
        )
        for i in range(30)
    ]
    parsed = _synthetic_doc(blocks, total_pages=6)
    chunked = chunk_document(parsed)

    assert len(chunked.chunks) > 1  # 确保确实跨越了多个块，order测试才有意义

    covered = [bi for c in chunked.chunks for bi in c.block_indices]
    assert covered == list(range(30))  # 恰好收录一次、顺序单调递增

    assert all(c.char_count <= config.CHUNK_SIZE_MAX for c in chunked.chunks)


# ---------------------------------------------------------------------------
# heading 推块规则
# ---------------------------------------------------------------------------

def test_heading_pushed_to_next_chunk():
    blocks = [
        ParsedBlock(page=1, block_index=0, text="X" * 80, block_type="paragraph", source_location="第1段"),
        ParsedBlock(page=1, block_index=1, text="H" * 10, block_type="heading", source_location="第2段"),
        ParsedBlock(page=1, block_index=2, text="Y" * 30, block_type="paragraph", source_location="第3段"),
    ]
    parsed = _synthetic_doc(blocks)
    chunked = chunk_document(parsed, chunk_size_target=100, overlap_blocks=0)

    # 不加规则的话：block0(80)+heading(10)=90<=100会被贪心加入，
    # 再加block2(30)才发现超限(120>100)，heading就会成为块尾——这是要避免的情况。
    assert len(chunked.chunks) == 2
    assert chunked.chunks[0].block_indices == [0]
    assert chunked.chunks[1].block_indices == [1, 2]  # heading 被推到下一块，且作为开头


# ---------------------------------------------------------------------------
# 超长 block 切分例外
# ---------------------------------------------------------------------------

def test_oversized_block_split_at_sentence_boundary():
    sentence = "这是一个用于测试超长文本切分逻辑的句子。"  # 20字
    long_text = sentence * 400  # 8000字，远超默认 CHUNK_SIZE_MAX=4500
    blocks = [
        ParsedBlock(page=1, block_index=0, text=long_text, block_type="paragraph", source_location="第1段"),
    ]
    parsed = _synthetic_doc(blocks)
    chunked = chunk_document(parsed)

    assert len(chunked.chunks) >= 2
    # 该 block_index 出现在多个chunk的 block_indices 里（明确允许的例外）
    chunks_with_block0 = [c for c in chunked.chunks if 0 in c.block_indices]
    assert len(chunks_with_block0) >= 2

    def _body_text(c):
        if c.text.startswith(config.CHUNK_OVERLAP_MARK):
            return c.text.split(config.CHUNK_BODY_MARK + "\n", 1)[1]
        return c.text

    # 各片段拼接能还原原文，且每段都切在句末（不破坏语义单元）
    reconstructed = "".join(_body_text(c) for c in chunked.chunks)
    assert reconstructed == long_text
    for c in chunked.chunks:
        assert _body_text(c).endswith("。")

    assert any("切分" in w and "0" in w for w in chunked.warnings)


def test_table_blocks_excluded_from_chunking():
    """补丁回归测试：block_type=="table" 的区域整体不进入任何 chunk（不送去LLM校对）。

    起因：真实文档（record_id=17诊断）里table类区域绝大多数是说明性UI截图/
    菜单结构图，不是待校对正文；真实数据显示这类block贡献了77%的"确定性错误"
    层假错误。跳过整类block比"识别后再降级"更直接。
    """
    table_text = "格" * 5000  # 即使超过 CHUNK_SIZE_MAX=4500 也应被跳过，不触发切分/独占逻辑
    blocks = [
        ParsedBlock(page=1, block_index=0, text="正文一。", block_type="paragraph", source_location="第1页"),
        ParsedBlock(page=1, block_index=1, text=table_text, block_type="table", source_location="第1页表格"),
        ParsedBlock(page=1, block_index=2, text="正文二。", block_type="paragraph", source_location="第1页"),
    ]
    parsed = _synthetic_doc(blocks)
    chunked = chunk_document(parsed)

    all_block_indices = {bi for c in chunked.chunks for bi in c.block_indices}
    assert 1 not in all_block_indices  # 表格block未出现在任何chunk里
    assert all_block_indices == {0, 2}
    assert not any(table_text in c.text for c in chunked.chunks)
    assert not any("表格" in w for w in chunked.warnings)  # 不再产生表格相关警告


def test_document_with_only_table_blocks_produces_no_chunks():
    blocks = [
        ParsedBlock(page=1, block_index=0, text="表格内容", block_type="table", source_location="第1页表格"),
    ]
    parsed = _synthetic_doc(blocks)
    chunked = chunk_document(parsed)
    assert chunked.chunks == []


# ---------------------------------------------------------------------------
# 重叠区 + 定位
# ---------------------------------------------------------------------------

@pytest.fixture
def overlap_scenario():
    blocks = [
        ParsedBlock(
            page=i + 1,
            block_index=i,
            text=chr(ord("A") + i) * 50,
            block_type="paragraph",
            source_location=f"第{i}段",
        )
        for i in range(6)
    ]
    parsed = _synthetic_doc(blocks, total_pages=6)
    chunked = chunk_document(parsed, chunk_size_target=110, overlap_blocks=2)
    return parsed, chunked


def test_overlap_prefix_and_text_format(overlap_scenario):
    _, chunked = overlap_scenario
    assert len(chunked.chunks) > 1

    assert chunked.chunks[0].overlap_prefix_blocks == []
    assert not chunked.chunks[0].text.startswith(config.CHUNK_OVERLAP_MARK)

    for idx in range(1, len(chunked.chunks)):
        prev_chunk = chunked.chunks[idx - 1]
        cur_chunk = chunked.chunks[idx]

        expected_overlap = prev_chunk.block_indices[-chunked.overlap_blocks :]
        assert cur_chunk.overlap_prefix_blocks == expected_overlap

        assert cur_chunk.text.startswith(config.CHUNK_OVERLAP_MARK)
        assert config.CHUNK_BODY_MARK in cur_chunk.text
        body = cur_chunk.text.split(config.CHUNK_BODY_MARK + "\n", 1)[1]

        by_index = {b.block_index: b for b in overlap_scenario[0].blocks}
        expected_body = "".join(by_index[bi].text for bi in cur_chunk.block_indices)
        assert body == expected_body

    assert all(c.char_count <= config.CHUNK_SIZE_MAX for c in chunked.chunks)

    # 完整性：正文归属（block_indices）互不重叠、覆盖全部block
    covered = [bi for c in chunked.chunks for bi in c.block_indices]
    assert covered == list(range(6))


def test_page_range_includes_overlap(overlap_scenario):
    _, chunked = overlap_scenario
    # chunk1 正文是 block2（第3页），重叠区是 block0/1（第1、2页）
    chunk1 = chunked.chunks[1]
    assert chunk1.overlap_prefix_blocks == [0, 1]
    assert chunk1.block_indices == [2]
    assert chunk1.page_range == (1, 3)


def test_locate_block_and_chunk_for_block(overlap_scenario):
    parsed, chunked = overlap_scenario

    assert locate_block(parsed, 0) == "第0段"
    with pytest.raises(ValueError):
        locate_block(parsed, 999)

    # block0 只作为chunk0的正文归属；即使它后续被chunk1当作重叠区引用，
    # chunk_for_block 也必须返回它真正的归属块(chunk0)
    assert chunk_for_block(chunked, 0) in {c.chunk_index for c in chunked.chunks if 0 in c.block_indices}
    for c in chunked.chunks:
        for bi in c.block_indices:
            assert chunk_for_block(chunked, bi) == c.chunk_index

    with pytest.raises(ValueError):
        chunk_for_block(chunked, 999)


# ---------------------------------------------------------------------------
# 真实样本冒烟
# ---------------------------------------------------------------------------

def test_real_sample_smoke():
    parsed = parse_document(SAMPLES_DIR / "sample_double_column.pdf")
    chunked = chunk_document(parsed)

    assert len(chunked.chunks) >= 1
    covered = [bi for c in chunked.chunks for bi in c.block_indices]
    # table类block不进入任何chunk（不送去LLM校对，见_build_fill_units文档），
    # 覆盖到的block集合应等于「非table的block」集合，不是全部block
    non_table_indices = [b.block_index for b in parsed.blocks if b.block_type != "table"]
    assert sorted(covered) == sorted(non_table_indices)
    assert len(covered) == len(set(covered))  # 真实样本没有超长block，不会触发切分例外
