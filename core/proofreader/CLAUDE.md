# core/proofreader/ 关键点

`core/llm_client.py::chat_completion()` 是唯一的LLM调用入口（详见 `core/llm_client.py` 模块docstring）；本包负责组装提示词、解析/校验LLM输出、把问题回填定位到原始 block_index/page_location。

## 目录结构

- `_types.py`：`RawIssue`/`ProofreadResult`/`LLMResponseError`。
- `prompt_builder.py`：系统提示词组装（`_build_system_prompt`/`_split_rules_by_number`，深度/精简两种模式）。
- `response_parser.py`：LLM输出解析与容错（`_strip_code_fence`/`_parse_json_array`/`_validate_item`）。
- `locator.py`：原文定位回填（`_contains`/`_split_body_overlap`/`_locate_block_for_snippet`）。
- `__init__.py`：`proofread_chunk()`/`proofread_document()` 编排入口，装配上述三块，对外暴露 `RawIssue`/`ProofreadResult`/`LLMResponseError`/`proofread_chunk`/`proofread_document`（`__all__`）。

**拆分子模块时需要注意的坑**：`tests/test_stage4.py` 用 `monkeypatch.setattr(proofreader, "chat_completion", ...)`/`monkeypatch.setattr(proofreader, "proofread_chunk", ...)` 直接打桩模块属性（而非用 `unittest.mock.patch` 按字符串路径），这类打桩只在被测函数与打桩目标位于**同一个模块的全局命名空间**时才生效——`proofread_chunk`/`proofread_document` 因此必须都留在 `__init__.py` 里（不能拆到子模块），且 `chat_completion` 必须在 `__init__.py` 里 `import`（而不是仅存在于 `core.llm_client` 里）。测试还直接访问 `proofreader._RETRY_HINT`/`proofreader._RULES_PATH`/`proofreader._split_rules_by_number`/`proofreader._build_system_prompt` 这几个私有名字，因此 `__init__.py` 把 `_RULES_PATH`/`_split_rules_by_number`/`_build_system_prompt` 从 `prompt_builder.py` 显式 re-export 进自己的命名空间，`_RETRY_HINT` 干脆直接定义在 `__init__.py`（这个常量只在 `proofread_chunk` 的重试循环里用，本就该跟 `proofread_chunk` 放一起）。这个坑与 [core/parser/CLAUDE.md](../parser/CLAUDE.md) 里 `ocr_pdf.py` 的 monkeypatch 目标问题是同一类。

## 与常规设计的偏差及原因

1. **`proofread_chunk(chunk, parsed)` 比"单chunk入单chunk出"的常规设计多一个 `parsed` 参数**——`Chunk` 不持有到 `ParsedDocument` 的反向引用，而 `page_location` 必须调 `locate_block(parsed, block_index)` 才能得到，是被 `core/chunker/` 的数据结构逼出的调整。
2. **LLM调用用 `requests` 直接发REST请求，没有引入 openai/dashscope SDK**——理由见 `requirements.txt` 里的注释（详见 `core/llm_client.py` 模块docstring）。

`proofread_chunk(chunk, parsed)` 组装系统提示词（`prompt/proofread_system.md` 模板 + `prompt/proofread_rules.md` 全文原样注入 + 动态注入 `config.CHUNK_OVERLAP_MARK`/`CHUNK_BODY_MARK`）调用LLM，解析JSON输出（剥离markdown围栏，非法JSON重试一次，两次都失败抛 `LLMResponseError`），逐条校验字段/枚举值丢弃脏数据，再把 `original_text` 回填定位到 `block_index`/`page_location`（正文区命中→定位；仅重叠区命中→丢弃；哪都找不到→保留但 `located=False`，留给 `core/classifier/` 处理）。`proofread_document(chunked, progress_callback=None)` 单块失败（`LLMCallError`/`LLMResponseError`）记录到 `ProofreadResult.chunk_warnings` 但不中断全文档。

## 并发执行设计决策

`proofread_document` 全部块一次性并发调用，不是顺序遍历：用 `ThreadPoolExecutor(max_workers=总chunk数)` 一次性提交所有块，不分批、不设人为并发上限。起因：8页文档3个chunk、串行约10分钟，100页文档会有三十多个chunk，串行下要接近2小时，要求"按一个block需要的时间完成"；能这么做是因为DashScope账号实测 RPM 15000/TPM 1200000，即使几百个chunk同时发请求也远碰不到限流，人为设并发上限（比如分两批）只会白白拖慢速度而不解决任何限流问题。

线程安全性：`chat_completion`（`core/llm_client.py`）和 `proofread_chunk` 内部都只用局部变量，没有共享可变状态，天然线程安全，不需要加锁；`proofread_document` 内部用 `dict[chunk位置index -> 结果]` 收集各线程结果，最后按 index 顺序拼回 `ProofreadResult`，保证输出顺序始终确定（不随线程完成顺序变化）；`progress_callback` 的计数递增发生在调用方主线程里遍历 `as_completed()` 的循环体中（不是在worker线程里），不存在计数竞态。

`tests/test_stage4.py::test_proofread_document_runs_chunks_concurrently` 专门验证并发（多块执行区间必须重叠、总耗时明显小于"块数×单块耗时"的串行值），不是只测"最终结果对不对"这种并发/串行都能通过的弱断言。

调试用 `tools/run_proofread.py <file> --max-chunks N`（会消耗真实API额度）。

## `glossary_text` 参数：接收格式化好的字符串，不接收 `core/glossary.py` 的数据结构

`proofread_chunk`/`proofread_document`/`_build_system_prompt` 都新增了 `glossary_text: str = ""` 参数（补丁：解决chunk间互不可见导致的跨块一致性误判，动机与产出逻辑见 `core/glossary.py` 模块docstring）。`core/workflow/run.py` 在分块后调用 `core/glossary.py::build_glossary(parsed)` 拿到 `list[GlossaryEntry]`，再用 `format_glossary_for_prompt()` 格式化成字符串，才传进 `proofread_document`。

**这里特意传字符串而不是 `GlossaryEntry` 列表**：`core/glossary.py` 需要 `import core.proofreader.response_parser` 复用JSON解析逻辑，如果本包反过来 `import core.glossary` 会构成循环导入（`core.proofreader` 包初始化会触发 `core.glossary` 初始化，`core.glossary` 又要初始化 `core.proofreader.response_parser`，取决于谁先被import，可能在 `GlossaryEntry` 类还没定义完时就被引用）。让 `core/proofreader/` 只认字符串参数（如同已经接受 `mode: str` 一样），彻底不知道 `core/glossary.py` 的存在，从根上避免这个坑。
