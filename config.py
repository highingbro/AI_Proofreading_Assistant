"""全局配置。

集中定义数据目录路径、分层常量、优先级常量、LLM API 相关配置占位。
"""

import os
from pathlib import Path

# ---------- 路径配置 ----------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
EXPORTS_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "app.db"

for _dir in (DATA_DIR, UPLOADS_DIR, EXPORTS_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

# ---------- 结果分层常量 ----------
LAYER_CONFIRMED = "确定性错误"
LAYER_DOUBTFUL = "存疑待核实"
LAYER_QUOTATION = "引文类"
LAYER_OPTIONAL = "风格可选"

LAYERS = (LAYER_CONFIRMED, LAYER_DOUBTFUL, LAYER_QUOTATION, LAYER_OPTIONAL)

# ---------- 优先级常量 ----------
PRIORITY_HIGH = "高"
PRIORITY_MEDIUM = "中"
PRIORITY_LOW = "低"
PRIORITY_OPTIONAL = "可选"

PRIORITIES = (PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW, PRIORITY_OPTIONAL)

# ---------- LLM API 配置占位（阶段4使用）----------
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "")

# ---------- 文档解析配置（阶段2使用）----------
# OCR 推理设备：'auto' 运行时自动检测（有可用GPU则用GPU，否则CPU）/'gpu'/'cpu'。
# GPU 上推理比 CPU 快一到两个数量级，且不走 CPU 那条有已知算子兼容问题的
# MKL-DNN+PIR 路径（见 core/parser.py 里对设备与 MKL-DNN 的处理）。
PADDLE_DEVICE = "auto"

# PDF 页面渲染为图片供 OCR 使用的分辨率（dpi）。分辨率越高OCR质量越好但越慢，
# GPU 下 250dpi 也很快；纯 CPU 环境如嫌慢可下调到 150。
OCR_RENDER_DPI = 250

# 一页可提取字符数低于该阈值，视为无文字层（需走OCR）
TEXT_LAYER_MIN_CHARS = 20

# A类（原生文字层PDF）分栏判断：在页面宽度这个比例区间内寻找空白分栏带
COLUMN_GAP_BAND = (0.4, 0.6)
# 分栏空白带最小宽度占页面宽度的比例，低于此值不视为有效分栏（排除噪声）。
# 真正的印刷双栏栏间距通常有明显宽度；单栏文档里偶尔出现的、由列表缩进等
# 造成的窄"伪分栏线"往往达不到这个宽度。
COLUMN_MIN_GAP_WIDTH_RATIO = 0.05
# 判定双栏还需候选分栏线两侧的文本字符数都达到总字符数的这个比例以上
# （真正的双栏内容会大致对半分布；一两个孤立块造成的伪分栏线两侧字符数
# 会严重失衡，据此过滤误判。按块宽度过滤"宽块"并不可靠——同一版面解析
# 模型对单栏/双栏文本的分块粒度本身就不稳定，窄块在两种情况下都很常见）
COLUMN_BALANCE_MIN_RATIO = 0.25

# B类（OCR）跨页检测：宽高比超过该阈值才可能是两页拼接的横版扫描
SPREAD_ASPECT_RATIO_THRESHOLD = 1.6
# 跨页检测：水平中线附近空白带宽度占页面宽度的比例超过该阈值，才判定为两页拼接
# （双栏排版的正常栏间距远小于此值，避免误伤双栏单页）
SPREAD_GAP_WIDTH_RATIO = 0.12

# OCR 单块结果过滤：置信度低于该阈值且文本长度低于 OCR_MIN_BLOCK_CHARS 才丢弃
OCR_MIN_BLOCK_CONFIDENCE = 0.6
OCR_MIN_BLOCK_CHARS = 4
# 整页平均置信度低于该阈值时记录 warning
OCR_PAGE_LOW_CONFIDENCE_THRESHOLD = 0.7

# 页眉页脚判定区域：页面顶部/底部该比例范围内的重复短文本视为页眉页脚
HEADER_FOOTER_ZONE_RATIO = 0.08

# PaddleOCR 识别语言
PADDLEOCR_LANG = "ch"
