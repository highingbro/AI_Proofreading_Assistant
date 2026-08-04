"""Streamlit 展示层。

放在顶层而不是 `core/ui/`：`core/` 不依赖 streamlit（`tools/` 下的调试脚本全是无头调用
core，`core/proofreader` 用 `progress_callback` 回调而不是自己画进度条），这条不变量要靠
包边界守住。`db/`（数据访问）、`core/`（业务逻辑）、`ui/`（展示）三层平级。

本包内任何模块都**不许在 import 时执行 `st.*`**，只定义函数和常量——`st.set_page_config`
必须是整个应用的第一个 Streamlit 调用，它在 `app.py` 里。设计背景见 `ui/CLAUDE.md`。
"""
