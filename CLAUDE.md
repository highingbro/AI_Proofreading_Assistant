# 出版校对AI助手

一个出版校对 AI 工具，用于替代"手动拖文档进大模型网页版 + 手动整理 Excel"的人工流程。技术栈：Python + Streamlit + SQLite。

原始需求/架构见 [项目框架文档_AI校对助手.md](项目框架文档_AI校对助手.md)——设计动机、四层结果分类的原因、各阶段验收标准，改动涉及分层规则或工作流设计时先看该文档。


## 目录结构

```
app.py              Streamlit 入口（署名→任务闸门→侧边栏→路由）——渲染逻辑全在 ui/
config.py           全局配置：路径、任务状态常量、分层/优先级常量、LLM配置、结果分层阈值、追问上下文配置、反馈学习阈值、OCR相关参数

core/
  parser/           文档解析——PDF/Word 统一解析，native_pdf.py(A类)/ocr_pdf.py(B类)/docx_parser.py(C类)/_columns.py/_common.py/_cjk_variants.py/_glyphs.py/_glyph_repair.py/_paragraphs.py/_types.py，详见 core/parser/CLAUDE.md
  chunker/          长文档分块——_types.py/fill_units.py/greedy_fill.py/locate.py，详见 core/chunker/CLAUDE.md
  llm_client.py     LLM调用封装——设计决策见文件顶部 docstring
  proofreader/      校对提示词组装 + 单块/全文档校对——_types.py/prompt_builder.py/response_parser.py/locator.py，详见 core/proofreader/CLAUDE.md
  classifier/       结果分层——_types.py/heuristics.py/base_rules.py/modifier_rules.py/postprocess.py，详见 core/classifier/CLAUDE.md
  workflow/         标准校对流程编排——run.py/persist.py/status.py，详见 core/workflow/CLAUDE.md
  exporter.py       Excel导出——设计决策见文件顶部 docstring
  followup.py       对话追问——设计决策见文件顶部 docstring
  feedback.py       人工反馈原始记录——记录/撤销issue被拒绝的反馈，薄封装，设计决策见文件顶部 docstring
  feedback_rules.py 反馈语义总结——把历史拒绝记录交给LLM总结成规则注入校对提示词，设计决策见文件顶部 docstring
  comparer.py       原稿比对——归一化+全文档文本流+句子级diff，设计决策见文件顶部 docstring

db/
  database.py       SQLite连接与建表（tasks、records、issues、feedback、feedback_rules 五张表）+ 旧库列迁移
  models.py         数据访问函数（CRUD，支持传入 db_path 便于测试用临时库）

ui/                 Streamlit 展示层——theme.py(色板+样式注入)/cards.py(问题卡与指标，三个页面共用)/actions.py(共用副作用)/tasks.py(署名+任务选择屏)/page_standard.py/page_compare.py/page_history.py/page_feedback.py，详见 ui/CLAUDE.md

tests/              按 core/ 模块命名的测试文件（test_parser.py、test_chunker.py、test_classifier.py…）；
                    test_app_tasks.py 单独覆盖任务闸门（其余 AppTest 冒烟统一预置 session_state["task_id"] 跳过闸门）

tools/              调试与验收脚本，全部无头调用 core/，不启动 Streamlit
  preview_parse.py       手动预览 core/parser/ 解析结果
  preview_chunks.py      手动预览 core/chunker/ 分块结果
  run_proofread.py       串起 解析→分块→校对 全链路（耗API额度，--save-json 保存RawIssue供离线调分层规则）
  preview_classify.py    预览 core/classifier/ 分层结果（--from-json 离线模式，不耗额度）
  parse_noise_metrics.py 量化A类解析残留的噪声（句中换行/位置断行/括号错位/读不出字/弃审字数）+ dump全文本，改解析逻辑前后各跑一次，零成本
  glyph_repair_report.py 逐处报告"字形被字体映射成另一个汉字"的检测与还原（跑OCR，不耗LLM额度），改 core/parser/_glyph_repair.py 前后各跑一次
  issue_noise_report.py  按噪声分类对比两条校对记录的issue（N1括号错位/N2位置串断裂/N3长正文被栏宽切开/N3短单元格两行标题）——解析层改动的最终验收以数据库产出为标准

prompt/             提示词模板 + 分阶段开发提示词存档（阶段N那几份是历史开发记录，不随代码重命名）
  proofread_rules.md     既有校对规则原文（十类维度），原样引用，不要改写
  proofread_system.md    校对系统提示词模板（含占位符，组装时动态注入规则原文与分块重叠标记）
  followup_system.md     追问系统提示词模板（含占位符，组装时注入issue的原文/上下文/分层等字段）
  feedback_rules_system.md 反馈语义总结提示词模板
pytest.ini          注册 integration marker，默认 `pytest`/`pytest tests/` 自动跳过耗额度的集成冒烟
```

各模块的详细设计背景按模块拆到了各自的说明文件里，只在真正要改对应代码/排查对应问题时才需要读：

- 目录模块（`core/parser/`、`core/chunker/`、`core/proofreader/`、`core/classifier/`、`core/workflow/`、`ui/`）——读该目录下的 `CLAUDE.md`。
- 单文件模块（`core/llm_client.py`、`core/exporter.py`、`core/followup.py`、`core/feedback.py`、`core/feedback_rules.py`、`core/comparer.py`）——读文件顶部的 docstring。
- UI 设计决策（色彩语义、卡片布局、session_state 生命周期、按钮key惯例、历史记录页两处状态同步的坑等）——读 `ui/CLAUDE.md`。

## 数据层级：任务 → 校对轮次 → 问题

一件持续的校对工作（比如某本期刊）是一个 **task**，它下面的每一次校对/比对是一条
**record**，每条 record 下是若干 **issue**。使用流程是线性的：进页面先选任务或建任务，
选定之后才出现功能入口，四个页面都在当前任务的范围内工作（"历史记录"= 这个任务的历史）。
任务有 激活/已解决/已关闭 三个状态（`config.TASK_STATUS_*`），只影响任务列表的默认筛选，
不自动推进。

**署名**：`records.author` 记的是"这轮校对是谁做的"，所有
任务和记录都在同一个 `config.DB_PATH` 里，谁都看得到。同一轮校对必然由同一个人跑完并审完，
所以 issues 表不另存处置人。候选署名从 `records.author ∪ tasks.created_by` 现取
（`get_authors()`），不单建人员表。


## 设计铁律（改动分层/校对逻辑前必读）

1. **结果必须分层**：确定类 / 存疑类 / 引文类 / 风格类，贯穿展示、追问、标记、导出全链路。四个层级的常量定义在 `config.py`（`LAYER_*`）。
2. **引文类强制保护**：判断为引文的内容一律"原文照录，不建议改动"，即使 LLM 输出未遵守，`core/classifier/` 也要在系统层强制改写归层——不能只依赖提示词。
3. **事实性内容置信度分级**：涉及人名/职务/历史事实的修改建议，非"充分把握"一律降级为"存疑待核实"，禁止确定性结论。同样需要系统层兜底校验，不能只信 LLM 自报的置信度。

## 运行与测试

```bash
# 激活虚拟环境（已存在 .venv，Windows PowerShell）
.venv\Scripts\Activate.ps1

streamlit run app.py            # 启动应用
pytest tests/test_classifier.py # 跑指定模块的测试（不带 -m integration 时自动跳过耗额度的集成冒烟）
pytest -m integration           # 手动跑耗真实API额度的集成冒烟测试
```

## 开发约定

- 每个模块独立可测试，对应一个 `tests/test_<模块名>.py`。
- `db/models.py` 的函数都支持传入 `db_path` 参数，测试时传临时数据库路径，不要依赖 `config.DB_PATH` 指向的真实库。
- **文档/注释只写现在为什么这么设计**不写"以前怎样、后来改成怎样"的过程记述——这类变更历史交给 git commit message（英文、简短），不进代码注释，避免文件顶部随时间越堆越长、"现在到底是什么样"反而被历史叙事淹没。

