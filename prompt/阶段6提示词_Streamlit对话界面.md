# 阶段6 开发提示词:Streamlit 对话界面(标准校对流)

---

接上一阶段。解析(阶段2)、分块(阶段3)、LLM校对(阶段4)、结果分层(阶段5)已全部完成并验收。后端全链路 `parse_document → chunk_document → proofread_document → classify_issues` 已能从一份文件跑到一组分好层的 `ClassifiedIssue`。现在实现**阶段6:Streamlit 对话界面的标准校对流(app.py)**。

本阶段目标:**用户在界面上传一份 PDF/Word,点一下开始,看到带进度的校对过程,最终按分层分组、页码排序看到逐条问题卡,能对每条标"采纳/拒绝(带理由)",状态实时写入 SQLite**。对应框架文档第5.1节工作流一的第1、2、3、4、5、7、8步中除"追问"和"导出Excel"以外的全部环节。

**明确不做(留给后续阶段,本阶段只留入口占位):**
- **追问(阶段7)**:问题卡上不做追问输入框,`st.chat_input`/`followup.py` 一律不碰。
- **Excel导出(阶段8)**:页面上放一个"导出Excel"按钮占位即可(点击提示"该功能将在阶段8实现"),不调用 `core/exporter.py`(它仍是 `NotImplementedError` 占位)。
- **原稿比对(阶段9)/历史记录页完善(阶段10)**:侧边栏这两个入口保持阶段1骨架现状,不在本阶段动。

## 本阶段最关键的一件事:Streamlit 的 rerun 模型(先读这段再写代码)

Streamlit 每次用户交互(点任意按钮、改任意输入框)都会**从头到尾重新执行整个 app.py**。这带来两个必须正面处理的问题,处理不好会导致"每点一次采纳按钮就重新调一遍 LLM、把 API 额度烧光"这种灾难:

1. **昂贵的校对全链路(耗时数分钟、耗真实API额度)绝对不能在每次 rerun 时重跑。** 必须用 `st.session_state` 把 `ClassifiedResult` 缓存住:只有用户显式点"开始校对"、且当前没有已缓存结果时,才跑一次全链路;跑完把结果连同 `record_id` 一起存进 `st.session_state`。之后所有 rerun(采纳/拒绝、切分层筛选等)都只读 `st.session_state` 里的结果,不重跑。
2. **不要用 `st.cache_data`/`st.cache_resource` 缓存 LLM 校对过程。** 它按输入哈希缓存、语义上不适合"一次性、有副作用(写库、耗额度)、结果需按用户操作演进"的流程,容易在换文件/改参数时静默重跑。本阶段一律用 `st.session_state` 显式管理生命周期,不用 `st.cache_*`。

判定"是否需要重跑"的依据建议用**上传文件的标识**(文件名+大小,或内容哈希):`st.session_state` 里记住"当前已校对的文件标识",用户换了新文件就清空旧结果、允许重新校对;文件没变则复用缓存,点多少次按钮都不重跑。

## 配置约定(config.py)

**本阶段无必须新增的配置项。** 分层展示顺序直接用已有的 `config.LAYERS`(顺序恰好是 确定性错误→存疑待核实→引文类→风格可选,作为分组展示顺序合理)。若你希望把"每个分层用什么图标/颜色徽标""高优先级整行标红的颜色"这类纯展示样式集中可调,可以在 config.py 追加一个 `LAYER_BADGE`/`PRIORITY_COLOR` 映射(带中文注释),但这是可选优化,不是硬性要求——单人工具直接在 app.py 里写死展示样式也可接受。**不要**为本阶段新增任何影响校对/分层逻辑的配置。

## 本阶段任务

### 1. 编排层:把全链路封装成可测试的纯函数(core/workflow.py,新建)

**动机:** app.py 顶层是 Streamlit 脚本,`import app` 会触发整页渲染,没法直接单元测试。把"不依赖 Streamlit 的编排与落库逻辑"抽到一个独立模块 `core/workflow.py`,app.py 只负责 UI 与 `session_state` 生命周期。这样阶段6的核心逻辑(串链路、写库、改状态)可以脱离 Streamlit 运行时用 mock 测试。`workflow.py` 是本阶段新增的正式模块,不是占位(与 `followup.py`/`exporter.py`/`comparer.py` 的 `NotImplementedError` 占位不同)。

```python
def run_standard_proofread(file_path, progress_callback=None) -> ClassifiedResult:
    """标准校对全链路:parse_document → chunk_document → proofread_document → classify_issues。
    progress_callback(current_chunk, total_chunks) 透传给 proofread_document,供 UI 接进度条。
    纯编排,不写库、不碰 Streamlit。"""

def persist_result(result: ClassifiedResult, doc_name: str, doc_version: str = "",
                   task_type: str = "标准校对", db_path=None) -> tuple[int, list[int]]:
    """把分层结果落库:先 create_record(带 result.stats 里的分层统计),
    再对每条 issue 调 add_issue,返回 (record_id, [issue_id,...])(顺序与 result.issues 一一对应)。"""

def set_issue_status(issue_id: int, status: str, reject_reason: str | None = None,
                     record_id: int | None = None, db_path=None) -> None:
    """更新单条问题状态(封装 update_issue_status);若给了 record_id,顺带把该 record 的
    accepted_count/rejected_count 重算并 update_record_stats(保证记录表统计与明细表实时一致)。"""
```

要点:
- `run_standard_proofread` 里各步的异常(解析失败、`LLMCallError`、`LLMResponseError`)**向上抛**,由 app.py 捕获展示,不在编排层吞掉;但 `proofread_document` 内部单块失败本就不中断、记进 `ProofreadResult.chunk_warnings`,这些 warning 经 `classify_issues` 汇入 `ClassifiedResult.warnings`,编排层原样带出即可。
- `persist_result` 落库字段映射:
  - `records` 表统计列(`total_issues`/`count_confirmed`/`count_doubtful`/`count_quotation`/`count_optional`/`high_priority_count`)直接取自 `result.stats`(阶段5已保证字段名与建表列名逐一对齐)。`accepted_count`/`rejected_count` 初始为0,`result_path` 暂空(阶段8导出时再回填)。
  - 每条 `ClassifiedIssue` → `add_issue`:`page_location`←`issue.page_location`、`original_text`、`issue_type`、`priority`←`issue.priority`、`layer`←`issue.layer`、`suggestion`←`issue.suggestion`(注意用分层后**可能被改写过**的 suggestion,不是 `original_suggestion`)、`context_snippet` 暂存 `issue.reason` 或留空(追问用的上下文窗口是阶段7的事,本阶段不填 `followup_history`)。
- **关于"阶段6是否该写 records 表"的一处解读(实现时请照此办并在完成报告里说明):** 框架文档把"流程记录写入"归在阶段8、把"状态实时写入SQLite"归在阶段6,但 `issues` 表有 `record_id NOT NULL` 外键,不先建 record 就没法存 issue 和它的采纳/拒绝状态。因此本阶段 `persist_result` **会创建 record 并写入分层统计**(这是外键约束下存 issue 状态的前提),阶段8导出时只需回填 `result_path` 和最终的 `accepted_count`/`rejected_count`。这样重启应用后历史状态不丢,也为阶段10的历史记录页提供真实数据。

### 2. 标准校对界面(app.py 的"标准校对"分支)

替换现有 `app.py` 中 `if page == "标准校对"` 分支的占位(`st.info("该功能将在后续阶段实现。")`),实现完整交互。**侧边栏三入口结构、`init_db()`、"原稿比对"/"历史记录"两个分支保持现状不动。**

按以下顺序组织页面:

**2.1 上传与触发**
- `st.file_uploader` 接受 `pdf`/`docx`(单份)。用户上传后,把文件落盘到 `config.UPLOADS_DIR`(文件名可加时间戳前缀避免覆盖),得到磁盘路径供 `run_standard_proofread` 使用(解析层吃的是路径)。
- 计算并记住当前文件标识(文件名+大小或内容哈希)。若与 `session_state` 里"已校对文件标识"不同,清空上一份的校对结果/record_id/状态缓存。
- 一个"开始校对"按钮。点击且(当前文件尚无缓存结果)时,才进入 2.2 的校对流程。

**2.2 校对过程与进度**
- 用 `st.status`(或 `st.spinner` + `st.progress`)包裹 `run_standard_proofread` 调用。传入的 `progress_callback(current, total)` 里更新 `st.progress` 进度条与文字("正在校对第 {current}/{total} 块…")。
- 校对成功后:调 `persist_result` 落库,把返回的 `ClassifiedResult`、`record_id`、`issue_ids`、以及"文件标识"一并存入 `st.session_state`。
- 校对过程中的异常(解析失败、缺 API Key 触发的 `LLMCallError`、`LLMResponseError` 等)用 `try/except` 捕获,`st.error` 展示可读错误信息(例如缺 `DASHSCOPE_API_KEY` 时明确提示去设环境变量),**不让异常把整页打崩**。异常时不写库、不缓存结果,允许用户改正后重试。

**2.3 结果展示(核心)**
仅当 `session_state` 里有当前文件的缓存结果时渲染:
- **顶部统计条**:用 `st.metric` 或一行文字展示 `result.stats`——总问题数,及四个分层各自数量、高优先级数。
- **warnings 展示**:若 `result.warnings` 非空(含单块校对失败、跨块去重提示等),用 `st.warning` 折叠展示,让用户知道"有N块没跑成/有重复被合并",避免误以为结果完整。
- **按分层分组展示**:按 `config.LAYERS` 顺序,每个分层一个 `st.expander` 或 `st.subheader` 分区(区标题带该层问题数;某层为0则可不显示或显示"无")。`result.issues` 阶段5已按 `block_index` 排好序(即页码升序),分组时保持该顺序即可,不要重新打乱。
- **逐条问题卡**,每条展示:
  - 页码/位置(`page_location`;未定位的显示"未定位")、`issue_type`、分层徽标、优先级。
  - **高优先级(`priority=='高'`)问题视觉上突出**(整卡/整行标红或加醒目徽标),呼应框架文档第8章"高优先级整行标红"的一致体验。
  - 原文片段(`original_text`)与修改建议(`suggestion`,即分层后可能已改写的措辞;引文类此处应已是"原文照录,不建议改动"、存疑类应已是"存疑,建议人工核实…")。
  - 可选:一行小字展示 `layer_notes`(归层依据)供你自己核对,正式使用可折叠或隐藏。
  - **采纳/拒绝控件**:两个按钮"采纳"/"拒绝";点"拒绝"时出现(或旁置)一个理由输入框。点击后调 `set_issue_status(issue_id, "已采纳"/"已拒绝", reject_reason, record_id)` 写库,并把该条的状态存进 `session_state`(键用 issue_id),使 rerun 后按钮区能显示当前状态(如已采纳的卡片置灰/打勾)。**每张卡片的按钮/输入框 `key` 必须用 issue_id 保证全局唯一**,否则 Streamlit 会因 key 冲突报错或串状态。

**2.4 导出入口(占位)**
- 底部放一个"导出Excel"按钮。本阶段点击只 `st.info("Excel导出将在阶段8实现")`,**不调用 `core/exporter.py`**。保留这个入口是为了让界面结构与框架文档第4章前端层四要素(上传/消息流/问题卡/导出入口)对齐,阶段8直接接线即可。

### 3. 测试(tests/test_stage6.py,不耗API额度)

Streamlit UI 本身难做纯单元测试,把断言重心放在 `core/workflow.py` 的可测逻辑上,UI 层用 Streamlit 官方 `AppTest` 做冒烟:

**3.1 编排层单元测试(mock,不联网)**
- `run_standard_proofread`:mock 掉 `proofread_document`(或更底层的 `chat_completion`)返回构造好的 `ProofreadResult`,断言全链路把它正确交给 `classify_issues`、`progress_callback` 被按 `(current, total)` 调用、`ClassifiedResult.warnings` 原样带出。
- `persist_result`:传临时 `db_path`(遵循项目约定,不碰真实库),构造一个含各层若干条的 `ClassifiedResult`,断言 `records` 表统计列与 `result.stats` 逐一相等、`issues` 表条数正确、返回的 `issue_ids` 与 `result.issues` 一一对应且能在库里查到、写入的 `suggestion` 是改写后的值。
- `set_issue_status`:改一条为"已采纳"、一条为"已拒绝(带理由)",断言 `issues` 表状态/理由正确;若传了 record_id,断言 `records` 表 `accepted_count`/`rejected_count` 被同步重算。

**3.2 UI 冒烟(streamlit.testing.v1.AppTest,mock 掉校对全链路)**
- 用 `AppTest.from_file("app.py")`,mock `core.workflow.run_standard_proofread` 返回构造结果(**决不真实联网/耗额度**),模拟点击"开始校对",断言:页面不抛异常、统计条出现、问题卡数量与构造数据一致、点"采纳"按钮后对应状态被更新。
- 若 `AppTest` 对本项目某些交互(动态 key、expander)支持不足导致断言困难,允许退化为"仅断言初始渲染不崩溃 + 编排层测试覆盖交互逻辑",并在完成报告里说明哪些交互只能靠 3.1 覆盖、哪些走了 AppTest。

## 完成标准

1. `pytest tests/test_stage6.py` 全部通过(不联网、不耗额度)。
2. `pytest tests/`(不带 `-m integration`)全绿,确认阶段1-5无回归。
3. `streamlit run app.py` 手动走一遍标准校对:上传一份真实 PDF/Word(优先用 `sample_double_column.pdf`,与阶段4/5测试保持同一份)→ 点开始 → 看到进度 → 看到按分层分组、页码排序的问题卡 → 对几条点采纳/拒绝 → 重启应用后(或切走再切回)状态仍在(证明落库生效)。这一步的实际观感如实记录,不美化。
4. 反复点采纳/拒绝、切分层、rerun 多次,**确认 LLM 校对不被重复触发**(可临时在 `run_standard_proofread` 入口打一行 log 观察调用次数,应恰好每份文件一次)。
5. 缺 `DASHSCOPE_API_KEY` 或上传损坏文件时,页面给出可读错误提示、不崩溃。

## 约束

- 只新建/修改:`app.py`(实现"标准校对"分支)、`core/workflow.py`(新建)、`tests/test_stage6.py`(新建);如做可选展示配置则加 `config.py`。**不动** `core/parser.py`/`chunker.py`/`llm_client.py`/`proofreader.py`/`classifier.py`(阶段2-5已验收)、`db/`(阶段1建表与CRUD已够用,如确需新增查询函数须先说明理由)。
- **不碰阶段7+的占位模块**:`core/followup.py`/`exporter.py`/`comparer.py` 保持 `NotImplementedError` 现状,本阶段不实现追问、不实现导出、不实现比对。
- 昂贵校对全链路每份文件**只跑一次**,用 `st.session_state` 管理,禁止用 `st.cache_*` 缓存 LLM 流程。
- 每张问题卡的控件 `key` 用 issue_id 保证唯一。
- 高优先级问题视觉突出,分层分组顺序用 `config.LAYERS`,页码排序沿用阶段5已排好的顺序,不重排。
- 落库统计字段严格取自 `result.stats`,不在 UI 层重新数一遍(阶段5已保证与建表列对齐,重复计数只会引入不一致)。
- 展示的 `suggestion` 用分层后(可能已改写)的值,不要显示 `original_suggestion`;引文类必须显示"原文照录,不建议改动"这类保护性措辞,不得出现"应改为…"。

---

## 附:给你自己的提醒(不粘给 Claude Code)

1. **本阶段是第一次"全链路可见"**:前五阶段的成果第一次在界面上串起来给人看。重点盯两件事——(a)真实文档跑出来的问题卡观感是否可用(分层是否直觉、高优先级是否醒目、引文保护措辞是否到位),(b)Streamlit rerun 有没有被 `session_state` 管住(别烧额度)。功能对不对是其次,这两点决定了工具"能不能日常用"。
2. **跳过了阶段7(追问)**:按 CLAUDE.md 记录的开发顺序调整(6→8→7),做完阶段6直接做阶段8(Excel导出)。所以本阶段问题卡上**不要**顺手加追问框——不是忘了,是有意留到阶段7。留好数据地基即可:`issues` 表已有 `context_snippet`/`followup_history` 两列,阶段7直接用。
3. **落库与导出的分工**:阶段6建 record + 写 issues + 实时更采纳/拒绝状态;阶段8只回填 `result_path` 和最终统计、生成Excel。别在阶段6提前碰导出,也别把 record 创建拖到阶段8(否则 issue 的外键没处挂,状态就存不下来)。
4. **AppTest 若不好用别硬刚**:Streamlit 的测试框架对复杂动态界面支持有限。核心交互逻辑已经抽进 `core/workflow.py` 能用 mock 测透了,UI 冒烟哪怕只能断言"初始渲染不崩",也够本阶段验收——别为了 UI 测试覆盖率把交互逻辑硬塞回 app.py。
