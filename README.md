# 出版校对AI助手

这是一个出版校对AI工具，有助于替代"手动拖文档进大模型网页版 + 手动整理 Excel"的人工流程。

## 功能

- 文档解析：PDF（单/两/四栏自动识别、原生文字层/扫描OCR自动判断）、Word
- 解析伪影清理：排版软件导出的PDF坏字形映射，解析阶段便会成套处理掉
- 长文档自动分块（带重叠区），逐块调用 LLM 校对
- 两种校对模式：深度（十类维度全量校对）/ 精简（保留机械性错别字+语法结构+高敏感度红线检查）
- 校对结果分层：错误类 / 存疑类 / 引文类 / 风格类
- 反馈学习：同一类问题被人工多次拒绝后，历史反馈会经LLM总结成规则注入后续校对提示词，减少反复报同类无意义问题
- 对话式追问：针对某条问题继续追问校对依据
- 原稿比对：原稿Word + 排版稿PDF/Word 逐句diff，识别实质性内容改动
- 任务管理：每次校对都可归属于某个任务，任务下可累积多轮记录
- Streamlit 界面：标准校对、原稿比对、历史记录、反馈学习管理
- 署名：每条记录标注完成人，供多人协作时追溯是谁做的（不是登录账号）
- Excel 导出

## 安装与启动

需要 Python 3.11。

```bash
git clone https://github.com/highingbro/AI_Proofreading_Assistant.git
cd AI_Proofreading_Assistant

# 创建并激活虚拟环境
python -m venv .venv
.venv\Scripts\Activate.ps1     # Windows PowerShell
source .venv/bin/activate      # macOS/Linux

# 安装依赖
pip install -r requirements.txt

# 单独安装 PaddlePaddle（扫描件OCR用，requirements.txt 装不了——
# 它要按本机有无GPU/CUDA版本选择不同的下载源）
pip install paddlepaddle==3.3.1                        # 纯CPU
# 有 NVIDIA GPU 时改装 GPU 版，例如 CUDA 12.6：
# pip install paddlepaddle-gpu==3.3.1 -i https://www.paddlepaddle.org.cn/packages/stable/cu126/

# 配置密钥（必填），LLM_BASE_URL / LLM_MODEL 可选，不设则用默认值
$env:DASHSCOPE_API_KEY = "sk-你的密钥"                  # Windows PowerShell
export DASHSCOPE_API_KEY="sk-你的密钥"                  # macOS/Linux

streamlit run app.py
```

首次启动会自动创建 `data/` 下的数据库和目录，浏览器打开后落在**任务选择界面**——新建一个任务
（比如"期刊A"）并进入，才会出现左侧的功能入口。

### 其他环境要求

- `config.py` 里 `PADDLE_DEVICE = "auto"` 会在运行时自动探测是否有可用 GPU，不需要手动改。
- 首次运行 OCR 会自动下载模型权重到 `~/.paddlex/official_models`，需要能访问外网；之后复用本地缓存。
- **PaddlePaddle 建议装上**，不只是扫描件才用得到：有文字层的 PDF 若含被字体映射坏的字形，
  解析时会渲染那几行送 OCR 读回原本的字体（见"解析伪影清理"），没装会中断解析。用不到的场景是
  只处理 Word、以及字形完全正常的 PDF；要在没装的机器上强制跳过，`parse_document(..., ocr="off")`。

## 数据存放位置


- `data/app.db`：所有任务、校对记录、问题明细、反馈学习数据
- `data/uploads/`：上传文件的临时落盘目录，仅供解析读取用，会自动清理旧文件
- `data/exports/`：导出的 Excel 文件
- `data/app.log`：运行日志


## 运行测试

```bash
pytest tests/                    # 跑全部测试（默认跳过耗真实API额度的集成冒烟）
pytest tests/test_classifier.py  # 跑指定模块的测试
pytest -m integration            # 手动跑耗真实API额度的集成冒烟测试
```

### 测试样例文档

| 文件名 | 内容 |
|---|---|
| `sample.pdf` | 任意**有文字层**的**单栏** PDF |
| `sample.docx` | 任意 Word 文档，正文非空 |
| `sample_single_column.pdf` | **无文字层的扫描件**（纯图片，如纸质件扫描/拍照转 PDF）、**单栏**、≥1 页 |
| `sample_double_column.pdf` | **无文字层的扫描件**、**双栏**、≥1 页，且单个版面区域文字量不超过 4500 字 |


