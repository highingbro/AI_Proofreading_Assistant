# 出版校对AI助手

单人使用、本地运行的出版校对 AI 工具，替代"手动拖文档进大模型网页版 + 手动整理 Excel"的人工流程。技术栈：Python + Streamlit + SQLite。

完整需求/架构见 [项目框架文档_AI校对助手.md](项目框架文档_AI校对助手.md)——设计动机、四层结果分类的原因、各阶段验收标准都在那里，改动涉及分层规则或工作流设计时先看该文档。

## 当前进度

按 [项目框架文档_AI校对助手.md](项目框架文档_AI校对助手.md) 第10章分阶段开发，每阶段提示词见 `prompt/阶段N提示词_*.md`。

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 项目骨架（目录、SQLite建表、配置） | ✅ 已实现 |
| 2 | 文档解析（PDF双栏/OCR、Word） | ✅ 已实现（`core/parser.py`） |
| 3 | 长文档分块 | ⬜ 仅函数签名（`core/chunker.py` 抛 `NotImplementedError`） |
| 4 | LLM调用封装 + 校对提示词 | ⬜ 仅函数签名（`core/llm_client.py`） |
| 5 | 结果分层（引文保护、事实置信度降级） | ⬜ 仅函数签名（`core/classifier.py`） |
| 6 | Streamlit对话界面（标准校对流） | ⬜ 未开始 |
| 7 | 对话式追问处理 | ⬜ 仅函数签名（`core/followup.py`） |
| 8 | Excel导出 | ⬜ 仅函数签名（`core/exporter.py`） |
| 9 | 原稿比对工作流 | ⬜ 仅函数签名（`core/comparer.py`） |
| 10 | 收尾（异常处理、历史页） | ⬜ 未开始 |

**未实现的模块目前只有函数签名和 docstring，函数体是 `raise NotImplementedError`——这是有意为之的阶段占位，不是 bug。** 除非明确要开发对应阶段，不要动这些函数体。

## 目录结构

```
app.py              Streamlit 入口（阶段1骨架，业务逻辑待接入）
config.py           全局配置：路径、分层/优先级常量、LLM配置占位、OCR相关参数
core/
  parser.py         文档解析（已实现）——PDF/Word 统一解析，见下方说明
  chunker.py        长文档分块（占位，阶段3）
  llm_client.py     LLM调用封装（占位，阶段4）
  classifier.py     结果分层（占位，阶段5）
  followup.py        对话追问（占位，阶段7）
  exporter.py        Excel导出（占位，阶段8）
  comparer.py         原稿比对（占位，阶段9）
db/
  database.py       SQLite连接与建表（records、issues 两张表）
  models.py         数据访问函数（CRUD，支持传入 db_path 便于测试用临时库）
tests/              每阶段一个测试文件（test_stage1.py、test_stage2.py…）
tools/preview_parse.py   手动预览 core/parser.py 解析结果的调试脚本
prompt/             各阶段开发提示词（给 Claude Code 分阶段下发用）
```

## core/parser.py 关键点（阶段2，已实现）

统一解析三类输入，PDF 按有无文字层分两条路径：

- **A类：有文字层PDF** —— PyMuPDF 按坐标提取文本块，手写双栏分栏检测 + 阅读顺序还原。分栏判定逻辑见 `config.py` 中 `COLUMN_GAP_BAND` / `COLUMN_MIN_GAP_WIDTH_RATIO` / `COLUMN_BALANCE_MIN_RATIO` 三个常量的注释——不是随手调的数字，改之前先看那段注释解释的误判场景。
- **B类：无文字层扫描PDF** —— 渲染为图片后用 PaddleOCR（版面检测 + 文本识别独立模型组合）+ paddlex 的 XY-Cut 算法还原阅读顺序。
- **C类：Word** —— python-docx 按段落顺序读取。

`config.PADDLE_DEVICE = "auto"`：运行时自动选 GPU/CPU。GPU 推理快一到两个数量级，且 CPU 路径下有已知的 MKL-DNN+PIR 算子兼容问题（细节见 `config.py` 里 `PADDLE_DEVICE` 上方注释和 `core/parser.py` 中对应处理）——排查 OCR 相关问题时留意这点。

调试解析效果用 `tools/preview_parse.py`，不必启动 Streamlit。

## 设计铁律（改动分层/校对逻辑前必读）

1. **结果必须分层**：确定性错误 / 存疑待核实 / 引文类 / 风格可选，贯穿展示、追问、标记、导出全链路。四个层级的常量定义在 `config.py`（`LAYER_*`）。
2. **引文类强制保护**：判断为引文的内容一律"原文照录，不建议改动"，即使 LLM 输出未遵守，`core/classifier.py`（阶段5）也要在系统层强制改写归层——不能只依赖提示词。
3. **事实性内容置信度分级**：涉及人名/职务/历史事实的修改建议，非"充分把握"一律降级为"存疑待核实"，禁止确定性结论。同样需要系统层兜底校验，不能只信 LLM 自报的置信度。

## 运行与测试

```bash
# 激活虚拟环境（已存在 .venv，Windows PowerShell）
.venv\Scripts\Activate.ps1

streamlit run app.py          # 启动应用
pytest tests/test_stage2.py   # 跑指定阶段的测试
```

## 开发约定

- 每个阶段独立可测试，对应一个 `tests/test_stageN.py`。开发新阶段前先看 `prompt/阶段N提示词_*.md`（如存在）和框架文档第10章对应行的验收标准。
- `db/models.py` 的函数都支持传入 `db_path` 参数，测试时传临时数据库路径，不要依赖 `config.DB_PATH` 指向的真实库。
