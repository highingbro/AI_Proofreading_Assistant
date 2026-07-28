"""B类：无文字层扫描 PDF 解析（从 core/parser.py 拆分而来，逻辑未改动）。

PaddleOCR 文本检测识别 + 独立版面检测 + XY-Cut阅读顺序还原，含跨页拼接检测。

说明：最初方案是直接调用 PPStructureV3 一体化管线，但在纯 CPU 环境
（paddlepaddle 3.3.1 CPU 版）下该管线的并行调度会段错误崩溃（exit code 139）。
逐一排查确认：独立的 LayoutDetection（版面检测）与 PaddleOCR（文本检测+识别）
两个模型单独调用都完全正常。因此改为自行组合这两个模型：
  1. LayoutDetection 得到区域框+标签（沿用与 PPStructureV3 相同的版面模型/标签体系）
  2. PaddleOCR 得到文字行框+文本+置信度
  3. 把文字行按中心点归属到对应版面区域，拼成整块文本、平均置信度
  4. 用 paddlex 内部真正的 XY-Cut 算法（sort_by_xycut，纯 numpy 函数，
     不经过模型执行器，没有崩溃风险）还原阅读顺序，双栏场景已内置正确处理

设备与 MKL-DNN：GPU 上推理快一到两个数量级，且不走 CPU 那条有已知算子兼容
问题的 MKL-DNN+PIR 执行路径。因此优先用 GPU；仅在回退到 CPU 时才需要禁用
MKL-DNN 来规避 ConvertPirAttribute2RuntimeAttribute 崩溃。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

import config
from core.parser._common import _detect_column_split, _source_location

_LAYOUT_PIPELINE = None
_OCR_PIPELINE = None
_PADDLE_DEVICE = None


def _resolve_device() -> str:
    """确定 paddle 推理设备（'gpu'/'cpu'），只解析一次并缓存。

    用模块级全局变量 _PADDLE_DEVICE 做缓存：第一次调用时才去检测GPU，
    检测结果记下来，之后同一进程内的调用直接复用，不用每次都重新探测。
    """
    global _PADDLE_DEVICE
    if _PADDLE_DEVICE is None:  # 还没检测过
        setting = config.PADDLE_DEVICE
        if setting == "auto":
            try:
                import paddle

                has_gpu = paddle.device.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0
                _PADDLE_DEVICE = "gpu" if has_gpu else "cpu"
            except Exception:
                # 检测过程本身出问题（如paddle没装对/环境异常），保守地退回CPU
                _PADDLE_DEVICE = "cpu"
        else:
            _PADDLE_DEVICE = setting  # 用户在config.py里手动指定了'gpu'或'cpu'，不做自动检测
    return _PADDLE_DEVICE


def _model_common_kwargs() -> dict:
    """版面检测/OCR 模型共用的设备相关初始化参数。"""
    device = _resolve_device()
    kwargs: dict = {"device": device}
    if device == "cpu":
        # 仅 CPU 需要禁用 MKL-DNN 以规避 paddlepaddle 的已知算子兼容崩溃。
        kwargs["enable_mkldnn"] = False
    return kwargs


def _get_layout_pipeline():
    """惰性加载版面检测模型，避免非OCR场景（native/docx）也承担模型加载开销。

    三个模块级变量 _LAYOUT_PIPELINE/_OCR_PIPELINE/_PADDLE_DEVICE（定义在本文件开头）
    起"惰性加载+缓存"的作用：只有真正走到OCR通道才会被赋值，且只加载一次
    （模型加载本身比较耗时，重复加载会拖慢速度）。
    """
    global _LAYOUT_PIPELINE
    if _LAYOUT_PIPELINE is None:  # 第一次用到才真正加载模型
        from paddleocr import LayoutDetection  # 延迟到函数内部才import，同样是为了避免非OCR场景也导入这个重量级依赖

        _LAYOUT_PIPELINE = LayoutDetection(**_model_common_kwargs())
    return _LAYOUT_PIPELINE  # 之后每次调用都直接复用已加载好的模型对象


def _get_ocr_pipeline():
    """惰性加载文本检测+识别模型。

    可选支持 config.PADDLEOCR_DET_MODEL/PADDLEOCR_REC_MODEL 覆盖默认识别模型
    （曾尝试用上一代最大档 PP-OCRv5_server 替换库默认的 PP-OCRv6_medium，
    动机是缓解低画质截图的字符误识别，见 core/parser/CLAUDE.md）。
    **真实A/B测试结论：两常量目前都保持 None（沿用库默认），因为 v5_server
    在真实文档同页对比中没有更准，个别字段反而更差，没有证据支持切换。**
    """
    global _OCR_PIPELINE
    if _OCR_PIPELINE is None:
        from paddleocr import PaddleOCR

        kwargs = dict(
            use_doc_orientation_classify=False,  # 不做文档方向自动分类（假设扫描件方向已经是正的）
            use_doc_unwarping=False,             # 不做文档扭曲矫正（假设扫描件基本平整）
            use_textline_orientation=False,      # 不做单行文字方向检测（假设都是水平排版）
            lang=config.PADDLEOCR_LANG,           # 识别语言，config.py里配置的是中文 "ch"
            **_model_common_kwargs(),
        )
        if config.PADDLEOCR_DET_MODEL:
            kwargs["text_detection_model_name"] = config.PADDLEOCR_DET_MODEL
        if config.PADDLEOCR_REC_MODEL:
            kwargs["text_recognition_model_name"] = config.PADDLEOCR_REC_MODEL
        _OCR_PIPELINE = PaddleOCR(**kwargs)
    return _OCR_PIPELINE


# paddlex 版面标签 -> 本模块 block_type 映射（见 paddlex.inference.pipelines.layout_parsing.setting.BLOCK_LABEL_MAP）
# LayoutDetection 模型给每个区域打的标签是它自己定义的一套名词（doc_title/paragraph_title等），
# 跟本模块统一的 block_type（'heading'/'paragraph'/...）不是一回事，这里就是两套命名之间的翻译表。
_HEADING_LABELS = {"doc_title", "paragraph_title", "abstract_title", "reference_title", "content_title"}
_PARAGRAPH_LABELS = {"text", "content", "abstract", "reference", "formula", "algorithm", "aside_text"}
_FOOTNOTE_LABELS = {"footnote"}
_TABLE_LABELS = {"table"}
_CAPTION_LABELS = {"table_title", "chart_title", "figure_title", "figure_table_chart_title"}
# 这些标签对应的区域直接丢弃：图片/图表/印章本身不是文字内容；页眉页脚/页码在B类OCR通道里
# 靠版面模型自己的标签直接识别丢弃，不需要像A类那样跨页比较文本重复度
_DROP_LABELS = {
    "image", "figure", "chart", "flowchart", "seal",
    "header", "header_image", "footer", "footer_image", "number",
}


def _label_to_block_type(label: str) -> str | None:
    """查表把版面模型的标签翻译成本模块统一的 block_type；返回 None 表示这个区域要丢弃。"""
    if label in _HEADING_LABELS:
        return "heading"
    if label in _PARAGRAPH_LABELS:
        return "paragraph"
    if label in _FOOTNOTE_LABELS:
        return "footnote"
    if label in _TABLE_LABELS:
        return "table"
    if label in _CAPTION_LABELS:
        return "figure_caption"
    if label in _DROP_LABELS:
        return None          # 调用方看到None就知道该丢弃这个区域
    return "other"            # 版面模型输出了未预料到的新标签，兜底归为"other"，不丢弃也不误分类


def _find_containing_region(cx: float, cy: float, regions: list[dict]) -> int | None:
    """给一段OCR识别出的文字行的中心点(cx,cy)，找它到底属于哪个版面区域(regions)。

    返回中心点落在其内部、面积最小（最精确）的区域下标。
    """
    candidates = []
    for i, r in enumerate(regions):
        x1, y1, x2, y2 = r["bbox"]
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            # 中心点确实落在这个区域框内部，记下(面积, 区域下标)作为候选
            candidates.append(((x2 - x1) * (y2 - y1), i))
    if not candidates:
        return None  # 没有任何区域框包含这个点（可能版面检测漏检了），交给调用方走"找最近区域"的兜底逻辑
    # 如果同一个点同时落在多个区域框内（区域框有重叠），优先选面积最小的——
    # 面积越小通常意味着这个区域框划得越精确，越可能是真正归属的区域
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def _find_nearest_region(cx: float, cy: float, regions: list[dict]) -> int:
    """找不到"包含这个点"的区域框时的兜底：找离这个点最近的区域框。"""
    best_i, best_dist = 0, None
    for i, r in enumerate(regions):
        x1, y1, x2, y2 = r["bbox"]
        # 计算点(cx,cy)到这个矩形框的最短距离：如果点已经在某个轴的范围内，
        # 那个轴上的距离就是0；否则是点到边界的差值。三元写法 max(x1-cx, 0, cx-x2)
        # 同时覆盖了"点在框左边"、"点在框内"、"点在框右边"三种情况
        dx = max(x1 - cx, 0, cx - x2)
        dy = max(y1 - cy, 0, cy - y2)
        dist = dx * dx + dy * dy  # 用距离的平方比较大小即可，不必开根号（省一次运算，效果一样）
        if best_dist is None or dist < best_dist:
            best_dist, best_i = dist, i
    return best_i


def _run_structure(img: Image.Image) -> tuple[list[dict], float | None, str | None]:
    """对一张页面图片，分别跑版面检测和OCR识别两个独立模型，再把两边结果拼到一起，
    并用 XY-Cut 算法还原出正确的阅读顺序。

    返回 (按阅读顺序排好的块列表, 整页平均置信度, 期刊页码)。期刊页码取自版面
    检测模型自己标注的 "number" 标签区域（页码/页眉页脚这类区域该模型本就单独
    打标，不需要像A类原生PDF那样靠正则猜形状），一页多个"number"区域时取第一个；
    该区域本就要被 _label_to_block_type 判 None 丢弃，这里只是丢弃前顺手记下文字。
    """
    # 延迟到函数内部才 import，避免模块加载时就承担这个依赖的开销
    from paddlex.inference.pipelines.layout_parsing.xycut_enhanced.xycuts import sort_by_xycut

    layout_pipeline = _get_layout_pipeline()
    ocr_pipeline = _get_ocr_pipeline()

    # 两个模型都要求传入图片文件路径（不是内存里的Image对象），所以先落到临时文件，
    # 用完自动清理（with tempfile.TemporaryDirectory() 会在退出时删除整个临时目录）
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = str(Path(tmp) / "page.png")
        img.save(tmp_path)
        layout_result = list(layout_pipeline.predict(tmp_path))[0]  # 版面检测结果：区域框+标签
        ocr_result = list(ocr_pipeline.predict(tmp_path))[0]        # OCR结果：文字行框+文本+置信度

    # 把版面检测出的每个区域框，转换成本函数内部用的字典结构，先准备好一个空的"lines"列表待填充
    regions = [{"label": b["label"], "bbox": tuple(b["coordinate"]), "lines": []} for b in layout_result["boxes"]]
    if not regions:
        return [], None, None  # 版面检测没识别出任何区域，直接返回空结果

    # 把OCR识别出的每一行文字，分配到它所属的版面区域里
    for text, score, box in zip(ocr_result["rec_texts"], ocr_result["rec_scores"], ocr_result["rec_boxes"]):
        if not text.strip():
            continue
        x1, y1, x2, y2 = box
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2  # 用这行文字的中心点判断它属于哪个区域
        region_idx = _find_containing_region(cx, cy, regions)
        if region_idx is None:
            # 没有任何区域框包含这个中心点（版面检测可能漏检），退而求其次找最近的区域
            region_idx = _find_nearest_region(cx, cy, regions)
        regions[region_idx]["lines"].append({"text": text, "score": float(score), "bbox": box})

    # 用paddlex内置的XY-Cut算法，根据各区域的坐标框，算出正确的阅读顺序（自动处理单栏/双栏）
    order = sort_by_xycut([r["bbox"] for r in regions], direction="horizontal", min_gap=1)

    blocks = []
    scores = []
    doc_page: str | None = None
    for idx in order:  # 按XY-Cut算好的顺序，依次处理每个区域
        region = regions[idx]
        block_type = _label_to_block_type(region["label"])
        # 区域内可能有多行文字，按"先上下(y)再左右(x)"排序后拼接成一段完整文本。
        # 用换行符而非空字符串拼接：不同行是版面上独立的一行（如多字段的表格/名单，
        # 常见于封面/版权页），完全不加分隔符会把毫不相关的几行文字直接粘成一串
        # 无法辨读的乱码式长句，让LLM把纯粹的解析伪影误判成错别字/语法问题。
        lines = sorted(region["lines"], key=lambda l: (l["bbox"][1], l["bbox"][0]))
        text = "\n".join(l["text"] for l in lines).strip()
        if region["label"] == "number" and text and doc_page is None:
            doc_page = text
        if block_type is None or not text:
            continue  # block_type为None表示这个标签本该丢弃（如图片区）；或者区域内没识别出任何文字，也丢弃
        # table类区域不做结构识别重建（曾用TableRecognitionPipelineV2重建行列结构，
        # 真实验证命中率接近零，见core/parser/CLAUDE.md）——这类区域绝大多数是说明性
        # UI截图/菜单结构图，不是待校对正文，core/chunker.py 会在分块阶段整体跳过
        # block_type=="table" 的block，不送去LLM校对，此处保留按坐标拉平的文本即可
        # （仅供未来展示/导出等场景使用，不再需要为校对准确性投入结构重建）。
        conf = sum(l["score"] for l in lines) / len(lines) if lines else None  # 该区域内所有行置信度的平均值
        if conf is not None:
            scores.append(conf)
        blocks.append({"text": text, "block_type": block_type, "bbox": region["bbox"], "confidence": conf})

    page_conf = sum(scores) / len(scores) if scores else None  # 整页平均置信度，供上层判断是否要写warning
    return blocks, page_conf, doc_page


def _ocr_and_order(img: Image.Image, force_layout: str) -> tuple[list[dict], float | None, str, str | None]:
    """在 _run_structure 的基础上，再做一遍"过滤低质量块"+"判断单双栏并标注column"，
    是 B类通道对外暴露的主要函数（core.parser._parse_pdf 直接调用它）。
    """
    raw_blocks, page_conf, doc_page = _run_structure(img)

    # 过滤明显的识别乱码：置信度很低 且 文字很短的块，大概率是图片背景/水印等干扰造成的误识别
    filtered = []
    for b in raw_blocks:
        conf = b["confidence"]
        if conf is not None and conf < config.OCR_MIN_BLOCK_CONFIDENCE and len(b["text"]) < config.OCR_MIN_BLOCK_CHARS:
            continue  # 极低置信度+极短文本，视为乱码丢弃
        filtered.append(b)

    if force_layout in ("single", "double"):
        mode = force_layout   # 手动指定分栏方式，跳过自动检测
        split_x = None
    else:
        # 自动检测时，只拿"看起来像正文/标题的块"去判断分栏——过滤掉表格/图注等，
        # 避免这些非正文内容的坐标干扰分栏线的判断
        text_like = [b for b in filtered if b["block_type"] in ("paragraph", "heading")]
        split_x = _detect_column_split(text_like, img.width)
        mode = "double" if split_x is not None else "single"

    if mode == "double":
        if split_x is None:
            split_x = img.width / 2  # 强制双栏但没有自动检测出精确分栏线时，退化为对半分
        for b in filtered:
            cx = (b["bbox"][0] + b["bbox"][2]) / 2
            b["column"] = "left" if cx < split_x else "right"
    else:
        for b in filtered:
            b["column"] = None  # 单栏，跟A类通道一样，统一置空保持字段结构一致

    return filtered, page_conf, mode, doc_page


# ---------------------------------------------------------------------------
# 跨页（横版两页拼接）检测与拆分
# ---------------------------------------------------------------------------

def _render_page_image(page: "fitz.Page", dpi: int) -> Image.Image:
    """把PDF某一页渲染成一张图片，供OCR使用（无文字层的页面只能靠"看图识字"）。"""
    import fitz  # 延迟import：避免非PDF场景（docx）也承担这个依赖开销

    zoom = dpi / 72  # PyMuPDF默认按72dpi渲染，这里换算成用户指定dpi对应的缩放倍数
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)  # 转成PIL Image对象，方便后续处理


def _find_pixel_gap(img: Image.Image, band: tuple[float, float]) -> tuple[float, float] | None:
    """在一张图片里，找中部有没有一条"竖直的、几乎全白"的空白带（用于跨页拼接检测）。

    跟A类的 _find_extent_gap 思路很像（都是找空白竖带），但作用对象不同：
    这里直接分析图片的像素颜色深浅，而不是文本块的坐标——因为跨页检测发生在
    渲染成图片之后、真正OCR识别之前，此时还没有任何"文本块"坐标可用。
    """
    gray = np.asarray(img.convert("L"), dtype=np.float32)  # 转成灰度图，每个像素是一个0~255的亮度值
    dark = gray < 200            # 亮度低于200的像素判定为"暗"（有墨迹/文字的地方通常比纯白背景暗）
    col_density = dark.mean(axis=0)  # 按列(axis=0是竖直方向)求平均，得到每一列"暗像素占比"
    w = col_density.shape[0]     # 图片宽度（像素数）
    band_i0 = max(0, int(band[0] * w))
    band_i1 = min(w, int(band[1] * w))   # 只在图片中部 band 范围内找空白（比如中缝一般在页面正中间附近）
    threshold = 0.01  # 这一列的暗像素占比低于1%，就认为这一列"几乎全白"（视为空白）

    # 下面这段找"最长连续空白列区间"的逻辑，跟 _find_extent_gap 里找最长连续空白格子
    # 是完全相同的算法，只是这里的"格子"换成了图片的"像素列"
    best: tuple[int, int] | None = None
    start: int | None = None
    for i in range(band_i0, band_i1):
        if col_density[i] < threshold:
            if start is None:
                start = i
        elif start is not None:
            if best is None or (i - start) > (best[1] - best[0]):
                best = (start, i)
            start = None
    if start is not None:
        if best is None or (band_i1 - start) > (best[1] - best[0]):
            best = (start, band_i1)

    if best is None:
        return None  # band范围内没有找到空白列，说明没有跨页中缝
    return best[0] / w, best[1] / w  # 返回空白区间的起止位置，换算成占图片宽度的比例(0~1)方便后续判断


def _maybe_split_spread(img: Image.Image, spread_order: str, warnings: list[str], phys_page_no: int) -> list[Image.Image]:
    """检测一张页面图片是不是"横版两页拼接扫描"，是的话从中缝切成左右两张独立图片。

    正常情况（不是跨页）返回只含原图的单元素列表；是跨页的话返回[左图, 右图]。
    """
    aspect = img.width / img.height  # 宽高比：横版两页拼接的图片明显比正常单页更"扁宽"
    if aspect <= config.SPREAD_ASPECT_RATIO_THRESHOLD:
        return [img]  # 宽高比不够夸张，不太可能是两页拼接，当作普通单页处理

    # 宽高比够夸张了，进一步验证：中部(30%~70%范围)是否真的存在一条足够宽的空白竖带
    gap = _find_pixel_gap(img, band=(0.3, 0.7))
    if gap is None or (gap[1] - gap[0]) < config.SPREAD_GAP_WIDTH_RATIO:
        return [img]  # 没找到空白带，或空白带太窄（可能只是双栏页面正常的栏间距），不当作跨页处理

    split_x = int((gap[0] + gap[1]) / 2 * img.width)  # 空白带中点，换算回像素坐标，作为切割线位置
    left = img.crop((0, 0, split_x, img.height))       # 切出左半部分
    right = img.crop((split_x, 0, img.width, img.height))  # 切出右半部分
    warnings.append(
        f"第{phys_page_no}页检测为横版两页拼接，已从中缝拆分为两个逻辑页（左前右后），"
        f"请人工确认页面顺序{'（含首页，可能为封面）' if phys_page_no == 1 or spread_order == 'cover_first' else ''}"
    )
    return [left, right]  # 顺序固定"左前右后"，若是封面(cover_first)之类特殊顺序，交给上面的warning提示人工核对
