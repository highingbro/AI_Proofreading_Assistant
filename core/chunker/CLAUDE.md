# core/chunker/ 关键点

把 `ParsedDocument` 切成一组 `Chunk`：先按 block 边界贪心装填（`config.CHUNK_SIZE_TARGET`/`CHUNK_SIZE_MAX`），再逐块拼接前向重叠区（`config.OVERLAP_BLOCKS`，标记文案见 `config.CHUNK_OVERLAP_MARK`/`CHUNK_BODY_MARK`）。边界规则：block永不被从中间切断（超长普通段落允许在句末`。！？`切分，两侧片段共享同一个 block_index）；`heading` 若会落在块尾则推到下一块开头。`locate_block`/`chunk_for_block` 供后续问题定位回溯。调试用 `tools/preview_chunks.py`。

## 目录结构

- `_types.py`：`Chunk`/`ChunkedDocument` 数据结构。
- `fill_units.py`：block → 填充单元（`_FillUnit`/`_build_fill_units`/`_split_oversized_text`），table跳过规则的落点。
- `greedy_fill.py`：`_fill_body`，贪心装填 + heading 推块规则。
- `locate.py`：`locate_block`/`chunk_for_block` 定位辅助。
- `__init__.py`：`chunk_document()` 编排入口，装配上述四块并处理重叠区拼接，对外只暴露 `Chunk`/`ChunkedDocument`/`chunk_document`/`locate_block`/`chunk_for_block`（`__all__`）。

## `block_type=="table"` 的区域整体不进入任何chunk，不送去LLM校对

起因：真实文档诊断（见 [core/parser/CLAUDE.md](../parser/CLAUDE.md) "表格结构识别方案"一节）发现，"确定性错误"层77%(72/93)的问题定位在table类区域，且这些区域绝大多数是文档里插入的说明性UI截图（如菜单结构对比图），根本不是需要校对的正文——截图内容不该被当作作者撰写的文字挑错别字，无论OCR/表格结构识别得多准都治标不治本。

`fill_units.py::_build_fill_units` 遇到 `block_type=="table"` 直接跳过，不生成任何填充单元；`ParsedBlock.text` 本身不受影响（仍是解析阶段产出的文本，供未来展示/导出等场景使用），只是不再出现在任何 `Chunk.text` 里。

这是有意识的取舍（宁可漏判不复杂化）：如果文档里真的存在需要校对的表格化正文，会被这条规则连带跳过——目前没有可靠信号能区分"表格化正文"和"截图/示意图"（LayoutDetection只给"table"一个标签，不含语义），留给以后真遇到这种文档再解决。

测试见 `tests/test_chunker.py`"block_type=="table"的区域整体不进入任何chunk"一节两条用例（`test_table_blocks_excluded_from_chunking`/`test_document_with_only_table_blocks_produces_no_chunks`），以及真实样本冒烟测试 `test_real_sample_smoke` 里"覆盖到的block集合应等于非table的block集合"这条断言。

## 字形读不出的位置：只排除**那一句**，不是整块

`ParsedBlock.text` 里出现 `config.NATIVE_UNREADABLE_GLYPH_MARK` 表示"此处原文有一个字，但它的
字形无法从文件里读出"——成因与还原尝试见 [core/parser/CLAUDE.md](../parser/CLAUDE.md)"字体把
字形映射成'另一个毫不相干的汉字'"一节。这些位置**没法校对**：字形读不出来，就无从判断作者有
没有写错。送进 LLM 只会得到两类假问题——把记号本身报成错别字，或者对着缺字的句子猜一条"漏字"。

`fill_units.py::_drop_unreadable_clauses` 因此在 `_build_fill_units` 里把含记号的那一句整句
排除；块内找不到任何切分点时（标题、条目那类，本来就短）整块跳过。与 table 规则一样，只影响
送审文本，`ParsedBlock` 本身不动。

**按句而不是按块，是有实测依据的取舍**：最坏情况（字形还原全不生效）按句丢 14.9% 的正文、
按块要丢 20.3%；更要紧的是按块会让"某个 241 字长段里只有一个字读不出"赔上整段。真实路径下
（还原开着）残留很小——实测 98 处伪造字符还原后只剩 7 处，最终排除 35/10193 字（0.34%）。

**切分点用 `_CLAUSE_END` 而不是超长block切分那个 `_SENTENCE_END`**：前者多收分号/省略号/换行，
目的是尽量缩小被排除的范围（切得越细丢得越少）；后者要的是语义完整的段。两者目标不同，
不共用一个正则。

测试见 `tests/test_chunker.py`"字形读不出的位置"一节四条用例，都做过反向验证。其中
`test_unreadable_clause_excluded_but_siblings_kept` 是★防线——防止实现退化成整块丢弃；
`test_text_without_mark_is_untouched` 防的是"顺手把正常文本也按句重组了"。
