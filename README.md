# 出版校对AI助手

本地运行的出版校对 AI 工具，替代"手动拖文档进大模型网页版 + 手动整理 Excel"的人工流程。

## 功能

- 文档解析：PDF（单/双栏、原生文字层/扫描OCR自动判断）、Word
- 长文档自动分块（带重叠区），逐块调用 LLM 校对
- 两种校对模式：深度（十类维度全量校对）/ 精简（保留机械性错别字+语法结构+高敏感度红线检查，适合容忍度较高的普通文档）
- 校对结果分层：确定性错误 / 存疑待核实 / 引文类 / 风格可选（引文强制保护、事实性内容置信度降级，系统层兜底，不只靠提示词）
- 反馈学习：同一类问题被人工多次拒绝后，历史反馈会经LLM总结成规则注入后续校对提示词，减少反复报同类无意义问题
- 对话式追问：针对某条问题继续追问校对依据
- 原稿比对：原稿Word + 排版稿PDF/Word 逐句diff，识别实质性内容改动
- Streamlit 界面：标准校对、原稿比对、历史记录（可查看/继续操作任意历史流程）、反馈学习管理
- 用户名隔离：按用户名分开保存各自的校对记录/上传文档，不填用户名默认沿用同一份数据
- Excel 导出

## 启动方式

```bash
# 创建虚拟环境（首次运行）
python -m venv .venv

# 激活虚拟环境
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 启动应用前需设置环境变量 DASHSCOPE_API_KEY（DashScope密钥）
# LLM_BASE_URL / LLM_MODEL 可选，不设置则使用默认值
streamlit run app.py
```



```

`config.py` 里 `PADDLE_DEVICE = "auto"` 会在运行时自动探测是否有可用 GPU

### 其他环境要求

- 首次运行 OCR 会自动下载模型权重到 `~/.paddlex/official_models`，需要能访问外网；之后复用本地缓存。


多人共用同一份部署时，建议每个人在侧边栏"当前用户"里填自己的名字——各自的校对记录和上传文档会分开保存

## 数据存放位置

- `data/app.db`（或用户名隔离下的 `data/app_<用户名>.db`）：所有校对记录、问题明细、反馈学习数据
- `data/uploads/`：上传文件的临时落盘目录，仅供解析读取用，会自动清理旧文件
- `data/exports/`：导出的 Excel 文件
- `data/app.log`：运行日志



## 运行测试

```bash
pytest tests/               # 跑全部阶段测试（默认跳过耗真实API额度的集成冒烟）
pytest tests/test_stage5.py # 跑指定阶段的测试
pytest -m integration       # 手动跑耗真实API额度的集成冒烟测试
```
