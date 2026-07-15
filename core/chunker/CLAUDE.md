# core/chunker/ 关键点（阶段3实现，阶段N做了目录拆分）

把 `ParsedDocument` 切成一组 `Chunk`：先按 block 边界贪心装填（`config.CHUNK_SIZE_TARGET`/`CHUNK_SIZE_MAX`），再逐块拼接前向重叠区（`config.OVERLAP_BLOCKS`，标记文案见 `config.CHUNK_OVERLAP_MARK`/`CHUNK_BODY_MARK`）。边界规则：block永不被从中间切断（超长普通段落允许在句末`。！？`切分，两侧片段共享同一个 block_index）；`heading` 若会落在块尾则推到下一块开头。`locate_block`/`chunk_for_block` 供阶段5/8做问题定位回溯。调试用 `tools/preview_chunks.py`。

## 目录拆分

- `_types.py`：`Chunk`/`ChunkedDocument` 数据结构。
- `fill_units.py`：block → 填充单元（`_FillUnit`/`_build_fill_units`/`_split_oversized_text`），table跳过补丁的落点。
- `greedy_fill.py`：`_fill_body`，贪心装填 + heading 推块规则。
- `locate.py`：`locate_block`/`chunk_for_block` 定位辅助。
- `__init__.py`：`chunk_document()` 编排入口，装配上述四块并处理重叠区拼接，对外只暴露 `Chunk`/`ChunkedDocument`/`chunk_document`/`locate_block`/`chunk_for_block`（`__all__`）。

原本是单文件 `core/chunker.py`（268行），按 `core/parser/` 先例拆分——内部四个关注点（数据结构/填充单元生成/贪心装填/定位查询）职责清晰、彼此依赖单向，拆分本身不涉及任何逻辑变更。测试全部通过公开API（`tests/test_stage3.py` 等未直接引用任何私有函数/子模块），拆分零回归、无需同步改测试。

## 补丁：`block_type=="table"` 的区域整体不进入任何chunk，不送去LLM校对

真实使用中发现后追加，非阶段3原始设计。起因：真实文档诊断（record_id=17，见 [core/parser/CLAUDE.md](../parser/CLAUDE.md) "已移除：表格结构识别补丁"一节）发现，"确定性错误"层77%(72/93)的问题定位在table类区域，且这些区域绝大多数是文档里插入的说明性UI截图（如菜单结构对比图），根本不是需要校对的正文——截图内容不该被当作作者撰写的文字挑错别字，无论OCR/表格结构识别得多准都治标不治本。

`fill_units.py::_build_fill_units` 遇到 `block_type=="table"` 直接跳过，不生成任何填充单元；`ParsedBlock.text` 本身不受影响（仍是解析阶段产出的文本，供未来展示/导出等场景使用），只是不再出现在任何 `Chunk.text` 里。之前"超长表格整块独占一个chunk"那条规则已随之删除（表格不再产生填充单元，这条规则无从触发）。

这是有意识的取舍（宁可漏判不复杂化）：如果文档里真的存在需要校对的表格化正文，会被这条规则连带跳过——目前没有可靠信号能区分"表格化正文"和"截图/示意图"（LayoutDetection只给"table"一个标签，不含语义），留给以后真遇到这种文档再解决。

测试见 `tests/test_stage3.py`"补丁回归测试：block_type=="table"的区域整体不进入任何chunk"一节两条用例（`test_table_blocks_excluded_from_chunking`/`test_document_with_only_table_blocks_produces_no_chunks`），以及真实样本冒烟测试 `test_real_sample_smoke` 里"覆盖到的block集合应等于非table的block集合"这条断言。
