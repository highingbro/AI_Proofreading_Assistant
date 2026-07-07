# 出版校对AI助手

单人使用、本地运行的出版校对 AI 助手（Python + Streamlit + SQLite）。
当前处于**阶段1：项目骨架**，仅包含目录结构、数据库表结构与最简页面骨架。

## 启动方式

```bash
# 激活虚拟环境（已存在 .venv）
# Windows PowerShell:
.venv\Scripts\Activate.ps1

# 安装依赖
pip install -r requirements.txt

# 启动应用
streamlit run app.py
```

## 运行测试

```bash
pytest tests/test_stage1.py
```
