# core/workflow/ 关键点

纯编排层，不依赖 Streamlit，供 `app.py` 调用，也便于脱离 Streamlit 运行时单独测试。

## 目录结构

- `run.py`：`run_standard_proofread()`，串起 parse→chunk→load_rejection_rules_text→proofread→classify全链路，返回 `(ClassifiedResult, ParsedDocument)` 元组（`ParsedDocument` 要传给 `persist_result` 算 `context_snippet`，只在本次调用链上存在，必须一并交出去）。`load_rejection_rules_text(db_path)`（`core/feedback_rules.py`）纯DB读取历史反馈的LLM语义总结规则（生成时机在"拒绝时"由 `app.py` 调用 `regenerate_rejection_rules`，不在这里），透传给 `proofread_document(..., rejection_rules_text=...)`，让LLM在生成建议这一步就规避曾被拒绝的问题模式，不由 `core/classifier` 事后改判，详见 `core/feedback_rules.py` 模块docstring；`run_standard_proofread` 保留 `db_path=None` 参数——本函数不写库，落库是 `persist_result` 单独负责的，这里只是把 `load_rejection_rules_text` 需要的库路径从调用方传进来。
- `persist.py`：`persist_result()` + `_build_context_snippet()`，落库+追问上下文窗口计算。
- `status.py`：`set_issue_status()` + `set_issue_note()`（批注与采纳/拒绝状态解耦，见下方"批注"小节）。
- `__init__.py`：re-export 上述四个公开函数（`__all__`）。

**拆分子模块时需要注意的坑**：`tests/test_app_standard_flow.py::test_run_standard_proofread_calls_pipeline_in_order`/`test_run_standard_proofread_forwards_mode_to_proofread_document` 用 `unittest.mock.patch("core.workflow.parse_document", ...)` 这类字符串路径直接测试 `run_standard_proofread()` 内部如何调用它的管线依赖（`parse_document`/`chunk_document`/`proofread_document`/`load_rejection_rules_text`/`classify_issues`）——`run_standard_proofread` 定义在 `core/workflow/run.py` 里，这些依赖名字也是在 `run.py` 自己的模块命名空间里 `import` 的，所以patch目标是 `core.workflow.run.parse_document` 等（不是 `core.workflow.parse_document`，否则 `unittest.mock.patch` 会因为该属性在 `core.workflow` 包上不存在而报错）。而 `patch("core.workflow.run_standard_proofread", ...)`/`persist_result`/`set_issue_status`/`set_issue_note`（`app.py` UI冒烟测试用来打桩顶层入口的那些）**不受这条规则影响**——`app.py` 是通过 `from core import workflow` + `workflow.run_standard_proofread(...)` 这种属性访问方式调用的，而不是 `from core.workflow import run_standard_proofread` 后裸调用，所以只要 `core/workflow/__init__.py` re-export 了这四个名字，`core.workflow.X` 属性就依然存在且能被正确patch。这个坑与 [core/parser/CLAUDE.md](../parser/CLAUDE.md)/[core/proofreader/CLAUDE.md](../proofreader/CLAUDE.md) 里记录的 monkeypatch 目标问题是同一类。

**`issues` 表 `record_id NOT NULL` 外键，决定了 `persist_result` 必须先建 `record` 再逐条写 issue**——`persist_result` 因此同时承担"建 record 写统计字段"和"写 issue 明细"两件事，`core/exporter.py` 导出时不需要再建 record，只回填 `result_path`（详见 `core/exporter.py` 模块docstring）。

## context_snippet 计算时机（为何不能等追问时才现算）

`issues` 表只存 `page_location`（人类可读TEXT，不可解析回整数），没存 `block_index`；"原文前后文窗口"必须靠 `block_index` 去 `ParsedDocument.blocks` 找相邻block，而 `ParsedDocument` 只在 `run_standard_proofread()` 执行期间存在于内存里，追问发生的时刻（可能是校对完成后无数次Streamlit rerun之后）根本拿不到。所以 `run_standard_proofread()` 返回 `(ClassifiedResult, ParsedDocument)` 元组；`persist_result()` 的 `parsed: ParsedDocument | None = None` 参数（放在 `db_path` 之前、其余默认参数之后）就是用来在落库当下算好 `context_snippet` 的。

`persist.py::_build_context_snippet(parsed, block_index)` 直接用列表切片 `parsed.blocks[block_index-N : block_index+N+1]`（`N=config.FOLLOWUP_CONTEXT_WINDOW_BLOCKS`，默认2）取窗口拼接——这依赖"`parsed.blocks[i].block_index == i` 恒成立"这个不变量，已通过读 `core/parser/` 三条通道（PDF原生/OCR/Word）的赋值逻辑验证：都是 `idx=0` 起步、每 `append` 一个block就 `idx+=1`，不会有跳号或乱序。`parsed is None` 或 `issue.block_index is None`（未定位）时 `context_snippet` 留空。

`tests/test_app_standard_flow.py::test_persist_result_computes_context_snippet_when_parsed_given` 真实构造 `ParsedDocument`+`ParsedBlock` 验证窗口拼接结果。

## 批注与采纳/拒绝状态解耦

批注（比如"已核实，确实需要修改"这类编辑判断依据）在采纳、拒绝、待处理任何状态下都可能想写，不该绑定在"拒绝"这一个动作上——`status.py::set_issue_note(issue_id, note, db_path=None)` 与 `set_issue_status` 各自只管自己那一列，互不影响。对应 `issues` 表列名是 `note`（`db/database.py::init_db()` 里的 `_migrate_reject_reason_to_note()` 负责把历史库的旧列名 `reject_reason` 原地迁移过来，幂等，重复调用不会报错）。

回归测试：`tests/test_project_skeleton.py::test_init_db_migrates_legacy_reject_reason_column_to_note`（构造带旧列名的库验证迁移+幂等）；`tests/test_app_standard_flow.py::test_set_issue_note_independent_of_status`（拒绝后写批注不影响status，status变化不清空批注）、`test_app_note_input_saves_independent_of_status`（AppTest驱动真实的 `on_change` 回调路径）。`app.py` 里对应的UI改动（批注输入框位置/独立于拒绝按钮）见 `app.py` 模块docstring。
