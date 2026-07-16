# 出版校对AI助手

本地运行的出版校对 AI 工具

## 功能

- 文档解析：PDF（单/双栏/OCR）、Word 
- 长文档自动分块，逐块调用 LLM 校对
- 校对结果分层：错误类 / 待核实类 / 引文类 / 风格类
- Streamlit 界面：标准校对、原告比对、历史记录
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

# 启动应用前需设置环境变量 DASHSCOPE_API_KEY，LLM_BASE_URL，LLM_MODEL
streamlit run app.py
```

## 运行测试

```bash
pytest tests/               # 跑全部阶段测试
pytest tests/test_stageX.py # 跑指定阶段测试
pytest -m integration       # 手动跑API的集成冒烟测试
```
