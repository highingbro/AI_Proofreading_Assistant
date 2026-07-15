# core/workflow/ 关键点（阶段6实现，阶段N做了目录拆分）

纯编排层，不依赖 Streamlit，供 `app.py` 调用，也便于脱离 Streamlit 运行时单独测试。

## 目录拆分

- `run.py`：`run_standard_proofread()`（阶段6，串起 parse→chunk→proofread→classify全链路）。
- `persist.py`：`persist_result()` + `_build_context_snippet()`（阶段6/阶段7，落库+追问上下文窗口计算）。
- `status.py`：`set_issue_status()`（阶段6）+ `set_issue_note()`（阶段8"批注"补丁）。
- `__init__.py`：re-export 上述四个公开函数（`__all__`）。

原本是单文件 `core/workflow.py`（133行），按 run/persist/status 三个关注点拆分（恰好对应阶段6/阶段7/阶段8三次改动落点），按 `core/parser/` 先例拆目录，纯粹的职责重排、无逻辑变更。

**唯一需要注意的坑**：`tests/test_stage6.py::test_run_standard_proofread_calls_pipeline_in_order`/`test_run_standard_proofread_forwards_mode_to_proofread_document` 用 `unittest.mock.patch("core.workflow.parse_document", ...)` 这类字符串路径直接测试 `run_standard_proofread()` 内部如何调用它的四个管线依赖（`parse_document`/`chunk_document`/`proofread_document`/`classify_issues`）——`run_standard_proofread` 现在定义在 `core/workflow/run.py` 里，这四个依赖名字也是在 `run.py` 自己的模块命名空间里 `import` 的，所以拆分后这四处patch目标必须改成 `core.workflow.run.parse_document` 等（不能再是 `core.workflow.parse_document`，否则 `unittest.mock.patch` 会因为该属性在 `core.workflow` 包上不存在而报错）。而 `patch("core.workflow.run_standard_proofread", ...)`/`persist_result`/`set_issue_status`/`set_issue_note` 这几个（`app.py` UI冒烟测试用来打桩顶层入口的那些）**不受影响、无需修改**——`app.py` 是通过 `from core import workflow` + `workflow.run_standard_proofread(...)` 这种属性访问方式调用的，而不是 `from core.workflow import run_standard_proofread` 后裸调用，所以只要 `core/workflow/__init__.py` re-export 了这四个名字，`core.workflow.X` 属性就依然存在且能被正确patch。这个坑与 `core/parser/` 拆分时 `ocr_pdf.py` 的 monkeypatch 目标问题、`core/proofreader/` 拆分时的同类坑是同一类，见 [core/parser/CLAUDE.md](../parser/CLAUDE.md)/[core/proofreader/CLAUDE.md](../proofreader/CLAUDE.md)。

## 阶段6→7→8 的签名演变

- `run_standard_proofread(file_path, progress_callback=None) -> ClassifiedResult`是阶段6原始签名；阶段7把返回值改成了 `(ClassifiedResult, ParsedDocument)` 元组——原因见下方"context_snippet 计算时机"。
- `persist_result(result, doc_name, doc_version="", task_type="标准校对", db_path=None) -> (record_id, issue_ids)`是阶段6原始签名；阶段7新增了 `parsed` 参数用于计算 `context_snippet`。写入的 `suggestion` 用分层后**已改写**的值（不是 `original_suggestion`）。
- `set_issue_status(issue_id, status, record_id=None, db_path=None)`原本带 `reject_reason` 参数，真实使用中发现后改成了独立的 `set_issue_note`，详见下方"补丁：批注"小节。

**框架文档把"流程记录写入"归阶段8、"状态实时写入SQLite"归阶段6，这里有一处必须先解决的矛盾**：`issues` 表 `record_id NOT NULL` 外键，不先建 `record` 就没地方挂 issue 状态。所以阶段6的 `persist_result` **提前**做了阶段8要做的"建record"这部分，阶段8只需回填 `result_path` 和导出相关逻辑，不用再建 record（详见 `core/exporter.py` 模块docstring）。

## context_snippet 计算时机（阶段7决策，为何不能等追问时才现算）

`issues` 表只存 `page_location`（人类可读TEXT，不可解析回整数），没存 `block_index`；"原文前后文窗口"必须靠 `block_index` 去 `ParsedDocument.blocks` 找相邻block，而 `ParsedDocument` 只在 `run_standard_proofread()` 执行期间存在于内存里，追问发生的时刻（可能是校对完成后无数次Streamlit rerun之后）根本拿不到。所以：`run_standard_proofread()` 的返回值从阶段6的单个 `ClassifiedResult` 改成 `(ClassifiedResult, ParsedDocument)` 元组；`persist_result()` 新增 `parsed: ParsedDocument | None = None` 参数（放在 `db_path` 之前、其余默认参数之后，不破坏现有关键字传参调用点）。

`persist.py::_build_context_snippet(parsed, block_index)` 直接用列表切片 `parsed.blocks[block_index-N : block_index+N+1]`（`N=config.FOLLOWUP_CONTEXT_WINDOW_BLOCKS`，默认2）取窗口拼接——这依赖"`parsed.blocks[i].block_index == i` 恒成立"这个不变量，已通过读 `core/parser/` 三条通道（PDF原生/OCR/Word）的赋值逻辑验证：都是 `idx=0` 起步、每 `append` 一个block就 `idx+=1`，不会有跳号或乱序。`parsed is None` 或 `issue.block_index is None`（未定位）时 `context_snippet` 留空，与阶段6原行为一致。

`tests/test_stage6.py::test_persist_result_computes_context_snippet_when_parsed_given` 真实构造 `ParsedDocument`+`ParsedBlock` 验证窗口拼接结果。返回值元组化牵动了 `tests/test_stage6.py`（两处mock返回值改元组）、`tests/test_stage7.py`、`tests/test_stage8.py`（导出按钮UI冒烟测试同样mock了 `run_standard_proofread`）里对应调用点的同步修正。

## 补丁：批注与状态解耦（真实使用中发现后追加，非阶段6/8原始设计）

起因：原设计里"拒绝理由"文本框只在点"拒绝"时才会被读取/写入（`reject_reason` 列），点"采纳"完全不碰它。真实使用中发现这个绑定关系不对——批注（比如"已核实，确实需要修改"这类编辑判断依据）在采纳、拒绝、待处理任何状态下都可能想写，不该只属于"拒绝"这一个动作。

改法：`issues` 表的 `reject_reason` 列**原地改名为 `note`**（不是新增列再废弃旧列——`data/app.db` 已有真实历史数据，直接改名保留原有内容，迁移逻辑见 `db/database.py::init_db()` 里的 `_migrate_reject_reason_to_note()`）。`db/models.py` 新增 `update_issue_note(issue_id, note, db_path=None)`（与 `update_issue_status` 各自只管自己那一列，互不影响）；`update_issue_status` 不再接受 `reject_reason` 参数。`status.py` 相应新增 `set_issue_note(issue_id, note, db_path=None)`，`set_issue_status` 签名同步去掉 `reject_reason`。

回归测试：`tests/test_stage1.py::test_init_db_migrates_legacy_reject_reason_column_to_note`（构造带旧列名的库验证迁移+幂等）；`tests/test_stage6.py::test_set_issue_note_independent_of_status`（拒绝后写批注不影响status，status变化不清空批注）、`test_app_note_input_saves_independent_of_status`（AppTest驱动真实的 `on_change` 回调路径）。`app.py` 里对应的UI改动（批注输入框位置/独立于拒绝按钮）见 `app.py` 模块docstring。
