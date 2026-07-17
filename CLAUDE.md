# 出版校对AI助手

单人使用、本地运行的出版校对 AI 工具，替代"手动拖文档进大模型网页版 + 手动整理 Excel"的人工流程。技术栈：Python + Streamlit + SQLite。

完整需求/架构见 [项目框架文档_AI校对助手.md](项目框架文档_AI校对助手.md)——设计动机、四层结果分类的原因、各阶段验收标准都在那里，改动涉及分层规则或工作流设计时先看该文档。

## 当前进度

按 [项目框架文档_AI校对助手.md](项目框架文档_AI校对助手.md) 第10章分阶段开发，每阶段提示词见 `prompt/阶段N提示词_*.md`。

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 项目骨架（目录、SQLite建表、配置） | ✅ 已实现 |
| 2 | 文档解析（PDF双栏/OCR、Word） | ✅ 已实现（`core/parser/`） |
| 3 | 长文档分块 | ✅ 已实现（`core/chunker/`） |
| 4 | LLM调用封装 + 校对提示词 | ✅ 已实现（`core/llm_client.py` + `core/proofreader/`） |
| 5 | 结果分层（引文保护、事实置信度降级） | ✅ 已实现（`core/classifier/`） |
| 6 | Streamlit对话界面（标准校对流） | ✅ 已实现（`core/workflow/` + `app.py`） |
| 8 | Excel导出 | ✅ 已实现（`core/exporter.py`） |
| 7 | 对话式追问处理 | ✅ 已实现（`core/followup.py`） |
| 9 | 原稿比对工作流 | ✅ 已实现（`core/comparer.py` + `core/workflow/`） |
| 10 | 收尾（异常处理、历史轮次查看页） | 🟡 历史记录详情页已实现（`app.py`）；异常处理已随阶段4/6分散实现（LLM重试、解析/校对异常兜底），未单独验收 |

**未实现的模块目前只有函数签名和 docstring，函数体是 `raise NotImplementedError`——这是有意为之的阶段占位，不是 bug。** 除非明确要开发对应阶段，不要动这些函数体。

## 目录结构

```
app.py              Streamlit 入口（标准校对流+Excel导出+追问+历史记录详情页+反馈学习管理+原稿比对）——UI设计决策见文件顶部 docstring
config.py           全局配置：路径、分层/优先级常量、LLM配置、结果分层阈值、追问上下文配置、反馈学习阈值、OCR相关参数
core/
  parser/           文档解析（已实现）——PDF/Word 统一解析，子目录拆成 native_pdf.py(A类)/ocr_pdf.py(B类)/docx_parser.py(C类)/_common.py/_types.py，详见 core/parser/CLAUDE.md
  chunker/          长文档分块（已实现）——子目录拆成 _types.py/fill_units.py/greedy_fill.py/locate.py，详见 core/chunker/CLAUDE.md
  llm_client.py     LLM调用封装（已实现，单文件）——设计决策见文件顶部 docstring
  proofreader/      校对提示词组装 + 单块/全文档校对（已实现）——子目录拆成 _types.py/prompt_builder.py/response_parser.py/locator.py，详见 core/proofreader/CLAUDE.md
  classifier/       结果分层（已实现）——子目录拆成 _types.py/heuristics.py/base_rules.py/modifier_rules.py/postprocess.py，详见 core/classifier/CLAUDE.md
  workflow/         标准校对流程编排（已实现）——子目录拆成 run.py/persist.py/status.py，详见 core/workflow/CLAUDE.md
  glossary.py       全局术语表构建（已实现，单文件）——解决chunk间互不可见导致的跨块一致性误判，设计决策见文件顶部 docstring
  exporter.py       Excel导出（已实现，单文件）——设计决策见文件顶部 docstring
  followup.py       对话追问（已实现，单文件）——设计决策见文件顶部 docstring
  feedback.py       人工反馈学习（已实现，单文件）——记录issue被拒绝的反馈、驱动 core/classifier 规则J自动降级，设计决策见文件顶部 docstring
  comparer.py       原稿比对（已实现，单文件）——归一化+段落对齐+句子级diff，设计决策见文件顶部 docstring
db/
  database.py       SQLite连接与建表（records、issues、feedback 三张表）
  models.py         数据访问函数（CRUD，支持传入 db_path 便于测试用临时库）
tests/              每阶段一个测试文件（test_stage1.py、test_stage2.py…）
tools/preview_parse.py    手动预览 core/parser/ 解析结果的调试脚本
tools/preview_chunks.py   手动预览 core/chunker/ 分块结果的调试脚本
tools/run_proofread.py    串起 解析→分块→校对 全链路，跑LLM调用的调试脚本（耗API额度，支持 --save-json 保存RawIssue供离线调分层规则）
tools/preview_classify.py 预览 core/classifier/ 分层结果的调试脚本（支持 --from-json 离线模式，不耗额度）
prompt/             各阶段开发提示词（给 Claude Code 分阶段下发用）
prompt/proofread_rules.md    既有校对规则原文（十类维度），原样引用，不要改写
prompt/proofread_system.md   校对系统提示词模板（含占位符，组装时动态注入规则原文与分块重叠标记）
prompt/followup_system.md    追问系统提示词模板（含占位符，组装时注入issue的原文/上下文/分层等字段）
pytest.ini          注册 integration marker，默认 `pytest`/`pytest tests/` 自动跳过耗额度的集成冒烟
```

各模块的详细设计背景（补丁起因、真实使用中发现的问题、测试策略）按模块拆到了各自的说明文件里，只在真正要改对应代码/排查对应问题时才需要读：

- 拆成目录的模块（`core/parser/`、`core/chunker/`、`core/proofreader/`、`core/classifier/`、`core/workflow/`）——读该目录下的 `CLAUDE.md`。
- 仍是单文件的模块（`core/llm_client.py`、`core/exporter.py`、`core/followup.py`、`core/glossary.py`、`core/feedback.py`、`core/comparer.py`）——读文件顶部的 docstring。
- `app.py` 的 UI 设计决策（session_state 生命周期、按钮key惯例、历史记录页两处状态同步的坑等）——读文件顶部的 docstring。

## 设计铁律（改动分层/校对逻辑前必读）

1. **结果必须分层**：确定性错误 / 存疑待核实 / 引文类 / 风格可选，贯穿展示、追问、标记、导出全链路。四个层级的常量定义在 `config.py`（`LAYER_*`）。
2. **引文类强制保护**：判断为引文的内容一律"原文照录，不建议改动"，即使 LLM 输出未遵守，`core/classifier/` 也要在系统层强制改写归层——不能只依赖提示词。
3. **事实性内容置信度分级**：涉及人名/职务/历史事实的修改建议，非"充分把握"一律降级为"存疑待核实"，禁止确定性结论。同样需要系统层兜底校验，不能只信 LLM 自报的置信度。

## 运行与测试

```bash
# 激活虚拟环境（已存在 .venv，Windows PowerShell）
.venv\Scripts\Activate.ps1

streamlit run app.py          # 启动应用
pytest tests/test_stage4.py   # 跑指定阶段的测试（不带 -m integration 时自动跳过耗额度的集成冒烟）
pytest -m integration         # 手动跑耗真实API额度的集成冒烟测试
```

## 开发约定

- 每个阶段独立可测试，对应一个 `tests/test_stageN.py`。开发新阶段前先看 `prompt/阶段N提示词_*.md`（如存在）和框架文档第10章对应行的验收标准。
- `db/models.py` 的函数都支持传入 `db_path` 参数，测试时传临时数据库路径，不要依赖 `config.DB_PATH` 指向的真实库。
- **模块拆分与文档记录的选择标准**：单文件超过约200行、或内部存在多个彼此独立又相互依赖顺序的关注点时，拆成 `core/模块名/` 目录（`_types.py` 放数据结构、按关注点拆若干私有子模块、`__init__.py` 做编排入口+对外`__all__`），详细设计背景写进该目录的 `CLAUDE.md`；否则保持单文件，设计背景写进文件顶部 docstring。拆目录时若已有测试用字符串路径 `unittest.mock.patch("core.模块名.某私有名字", ...)` 或 `monkeypatch.setattr(模块, "某私有名字", ...)` 直接打桩模块属性，要先确认被测函数和打桩目标是否会落在同一个子模块——这类打桩只在两者位于同一模块全局命名空间时才生效，拆分时若把被测函数和它依赖的名字拆到了不同子模块，需要同步把测试的打桩路径改成新的子模块路径（`core/parser/`、`core/proofreader/`、`core/workflow/` 的 CLAUDE.md 里各记录了一次真实踩坑案例）。
