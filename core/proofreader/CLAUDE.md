# core/proofreader/ 关键点

`core/llm_client.py::chat_completion()` 是唯一的LLM调用入口（详见 `core/llm_client.py` 模块docstring）；本包负责组装提示词、解析/校验LLM输出、把问题回填定位到原始 block_index/page_location。

## 目录结构

- `_types.py`：`RawIssue`/`ProofreadResult`/`LLMResponseError`。
- `prompt_builder.py`：系统提示词组装（`_build_system_prompt`/`_split_rules_by_number`，深度/精简两种模式）。
- `response_parser.py`：LLM输出解析与容错（`_strip_code_fence`/`_parse_json_array`/`_validate_item`）。
- `locator.py`：原文定位回填（`_contains`/`_split_body_overlap`/`_locate_block_for_snippet`）。
- `__init__.py`：`proofread_chunk()`/`proofread_document()` 编排入口，装配上述三块，对外暴露 `RawIssue`/`ProofreadResult`/`LLMResponseError`/`proofread_chunk`/`proofread_document`（`__all__`）。

**拆分子模块时需要注意的坑**：`tests/test_llm_proofreader.py` 用 `monkeypatch.setattr(proofreader, "chat_completion", ...)`/`monkeypatch.setattr(proofreader, "proofread_chunk", ...)` 直接打桩模块属性（而非用 `unittest.mock.patch` 按字符串路径），这类打桩只在被测函数与打桩目标位于**同一个模块的全局命名空间**时才生效——`proofread_chunk`/`proofread_document` 因此必须都留在 `__init__.py` 里（不能拆到子模块），且 `chat_completion` 必须在 `__init__.py` 里 `import`（而不是仅存在于 `core.llm_client` 里）。测试还直接访问 `proofreader._RETRY_HINT`/`proofreader._RULES_PATH`/`proofreader._split_rules_by_number`/`proofreader._build_system_prompt` 这几个私有名字，因此 `__init__.py` 把 `_RULES_PATH`/`_split_rules_by_number`/`_build_system_prompt` 从 `prompt_builder.py` 显式 re-export 进自己的命名空间，`_RETRY_HINT` 干脆直接定义在 `__init__.py`（这个常量只在 `proofread_chunk` 的重试循环里用，本就该跟 `proofread_chunk` 放一起）。这个坑与 [core/parser/CLAUDE.md](../parser/CLAUDE.md) 里 `ocr_pdf.py` 的 monkeypatch 目标问题是同一类。

## 与常规设计的偏差及原因

1. **`proofread_chunk(chunk, parsed)` 比"单chunk入单chunk出"的常规设计多一个 `parsed` 参数**——`Chunk` 不持有到 `ParsedDocument` 的反向引用，而 `page_location` 必须调 `locate_block(parsed, block_index)` 才能得到，是被 `core/chunker/` 的数据结构逼出的调整。
2. **LLM调用用 `requests` 直接发REST请求，没有引入 openai/dashscope SDK**——理由见 `requirements.txt` 里的注释（详见 `core/llm_client.py` 模块docstring）。

`proofread_chunk(chunk, parsed)` 组装系统提示词（`prompt/proofread_system.md` 模板 + `prompt/proofread_rules.md` 全文原样注入 + 动态注入 `config.CHUNK_OVERLAP_MARK`/`CHUNK_BODY_MARK`）调用LLM，解析JSON输出（剥离markdown围栏，非法JSON重试一次，两次都失败抛 `LLMResponseError`），逐条校验字段/枚举值丢弃脏数据，再把 `original_text` 回填定位到 `block_index`/`page_location`（正文区命中→定位；仅重叠区命中→丢弃；哪都找不到→保留但 `located=False`，留给 `core/classifier/` 处理）。`proofread_document(chunked, progress_callback=None)` 单块失败（`LLMCallError`/`LLMResponseError`）记录到 `ProofreadResult.chunk_warnings` 但不中断全文档。

## 并发执行设计决策

`proofread_document` 并发调用，不是顺序遍历：用 `ThreadPoolExecutor(max_workers=min(总chunk数, config.PROOFREAD_MAX_CONCURRENT_CHUNKS))` 分批提交所有块。起因：8页文档3个chunk、串行约10分钟，100页文档会有三十多个chunk，串行下要接近2小时，要求"按一个block需要的时间完成"。

**并发数必须设上限（`config.PROOFREAD_MAX_CONCURRENT_CHUNKS`，当前8），且判断依据不是账号配额**：DashScope账号实测 RPM 15000/TPM 1200000，几百个chunk同时发也碰不到账号限流——但账号配额约束的是"允许发多快"，不代表"服务端能同时处理多少个"，真正卡脖子的是后者。`data/app.log` 的实测：29个块一次性全发时，先完成的也要170~230s（低并发下同一天是47~66s），越晚完成的排队越久，一路拖到300~570s，其中16个块在同一秒集体撞上超时上限失败；失败重试同样一次性全发，等于把同一次拥堵原样重演。过了拥堵拐点后再加并发边际收益为负（排队时间增长快于吞吐提升，超时重试还会重新触发同等规模的拥堵），所以有上限的总耗时反而更短。8 是按日志间接推断的起点，不是精确压测得出。

线程安全性：`chat_completion`（`core/llm_client.py`）和 `proofread_chunk` 内部都只用局部变量，没有共享可变状态，天然线程安全，不需要加锁；`proofread_document` 内部用 `dict[chunk位置index -> 结果]` 收集各线程结果，最后按 index 顺序拼回 `ProofreadResult`，保证输出顺序始终确定（不随线程完成顺序变化）；`progress_callback` 的计数递增发生在调用方主线程里遍历 `as_completed()` 的循环体中（不是在worker线程里），不存在计数竞态。

`tests/test_llm_proofreader.py::test_proofread_document_runs_chunks_concurrently` 验证并发（多块执行区间必须重叠、总耗时明显小于"块数×单块耗时"的串行值），用4个chunk（低于并发上限，行为等同不设上限）。`test_proofread_document_caps_concurrency_at_config_limit` 专门验证块数超过上限时同时在跑的块数不超过 `config.PROOFREAD_MAX_CONCURRENT_CHUNKS`。

调试用 `tools/run_proofread.py <file> --max-chunks N`（会消耗真实API额度）。
