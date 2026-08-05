"""A类原生PDF：字体把字形映射成了**另一个毫不相干的汉字**时，检测出来并读回真身。

## 这是同一个病根的第三种坏法，前两种已有各自的处置

PDF 子集字体的 ToUnicode 反查表有缺陷时，`get_text()` 的产出有三种坏法：

| ToUnicode 的结果 | 肉眼所见 | 处置 |
|---|---|---|
| 映到码位不对的同形字（康熙部首 `⼯`） | 像正常汉字 | 归一化，见 `_cjk_variants.py` |
| 映不出任何字符 | 是图标，本就不是文字 | 删除，见 `native_pdf.py::_strip_unmapped_glyph_chars` |
| **映到一个毫不相干的字** | **完全正常** | **本模块** |

第三种最危险，因为**没有任何码位特征可查**：`每`→`嫥`、`能`→`腉` 落在生僻字上还算显眼，而
`数字化转型`→`侧㶵⻉转型` 里的 `侧` 是个再常用不过的汉字。人眼看 PDF 一切正常（字形画的是对的，
错的只是反查表），只有复制/提取文字时才错，于是 LLM 逐条报"错别字"——**报出来的错原文并不
存在，是我们自己解析出来的**。真实代价：记录79 的 15 条"确定性错误"里约 10 条是这么来的。

## 检测：精确信号，不是启发式

`get_texttrace()` 不走 ToUnicode 那条反查路径，映不出来时如实给 `U+FFFD`；`get_text()` 才会
顺着坏表编出一个字。两者**按字符原点坐标配对**，不一致的位置就是被编造的位置。

真实文档实测（`2026e-works媒体服务简介`，14页）：10347 个字符配上 10276 个（99.31%），没配上的
71 个**全是空格**（texttrace 不输出空格）；其中 195 处 texttrace 报未知，**其余位置两者零分歧**。

**必须按坐标配对，不能把两边拉平了按下标对齐**：同一页两种取法的字符数并不相等（实测 14 页里
8 页不等，差 2~20 个），按下标对齐会整体错位、把好字标成坏字。

## 还原：按行送OCR，且只在伪造位置采纳

**按行、不按字**。孤立渲染单个字形去比对/识别，全 CJK 候选下实测只有 65%，而且依赖参考字体
与原字体同源（换一家排版就塌）。按整行送识别模型则有上下文，与字体无关——真实反例
`CEO⻩㛅⽆⼠`＝`CEO黄培博士`，两个伪造字相邻，孤立比字形绝无可能认出来，整行识别一次就对。

**只在伪造位置采纳 OCR 的字，其余一律保留文字层**。这道约束把 OCR 的弱点关在笼子里：识别率
再差也只作用在那些本来就已经是错的位置上，**永远不可能把原本正确的文字改坏**。

**闸门**：该行**非伪造位置**上 OCR 与文字层的一致率达到 `config.NATIVE_GLYPH_REPAIR_MIN_LINE_AGREEMENT`
才采纳——这行别处都读不准，伪造位取它就不可信。达不到就当读不出来。

**算一致率前必须按生产口径规范化一份比较用文本**（去掉占位码位、跑 `normalize_cjk_variants`）。
不做的话 `⼚`/`⼾`/`⽹` 这些会被算成"OCR 与文字层不一致"，一致率分布从 {0.5:3, 0.9:30, 1.0:62}
掉到 {0.2:3, 0.7:4, 0.8:29, 0.9:44, 1.0:15}，闸门完全失真。这只是比较用的副本，不改变现有
归一化的时机。

**读不出来的落成 `config.NATIVE_UNREADABLE_GLYPH_MARK`**，由 `core/chunker/` 把含它的那一句
整句排除送审（不能直接删字符——`智能制造` 删两个字变 `智制`，会造出原文没有的词）。

真实文档效果：98 处伪造字符还原 95（97%），过闸门后 92（94%），目视核对**零误还原**；
全文档 668 行只有 46 行（6.9%）需要送 OCR。

## 必须自己补 `None` 的引用计数（PyMuPDF 1.27.2.2+ 的 bug）

`get_texttrace()` 每个 span 会**多减一次 `None` 的引用计数**：填充文字的 span 把 `linewidth`
字段填成 `None` 时借用了引用没有 INCREF，等这批 span 字典被释放时照常 DECREF，一来一去每个
span 净减一次。该字段在 1.26.x 里从不为 `None`，是 1.27.2.2 那次"修正 linewidth"（#4902）
引入的回归；1.28.0 已是最新版，上游没有修复版可换。

Python 3.11 的 `None` 不是不朽对象，初始引用计数约 12000，而每页有两三百个 span，**一份 36 页
的期刊单趟解析就足以把它减到 0**，解释器随即 `Fatal Python error: none_dealloc` 硬崩溃——
不是能 `try` 住的异常，而且发作点是此后任意一次垃圾回收，崩溃现场跟真凶毫无关系（实测表现为
"连跑到第 7 份文档时崩"，与那份文档无关）。

**不要为了绕开它把 PyMuPDF 降到 1.26.x**：实测降级会改变 PyMuPDF 自己的 block 切分，79期有
3 句退回词中断行、句中换行指标 5237→5239，踩坏 `_paragraphs.py` 那套照 1.28 切分标定的段落
合并。补偿的两个要点见 `_texttrace_fabricated_origins` 的注释。
"""

from __future__ import annotations

import ctypes
import difflib
import logging
import sys

import config
from core.parser._cjk_variants import normalize_cjk_variants

logger = logging.getLogger(__name__)

# 这两类字符 texttrace 同样报未知，但都**不是"被编造出来的字"**，不进本模块的处理范围：
# C0 占位码位由 native_pdf.py::_strip_unmapped_glyph_chars 整个删掉（是装饰图标）；
# 私有使用区是符号字体画的项目符号，承载"这是一个列表项"的语义，删或换都会丢信息。
def _is_out_of_scope(ch: str) -> bool:
    cp = ord(ch)
    return cp < 0x20 or cp == 0x7F or 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0x10FFFD


def _texttrace_fabricated_origins(page) -> set[tuple[float, float]]:
    """`get_texttrace()` 报未知的那些字符原点坐标，并补回它漏减的 `None` 引用计数。

    补偿有两个容易做错的地方：

    1. **必须在 span 结构释放之后才量差值**。泄漏发生在字典析构时而不是调用时，紧挨着调用去量
       只能量到 0（试过，补偿完全失效）。所以先把要的数据抄出来、`del` 掉整批 span，再量。
       用推导式而不是 `for` 循环，是为了不在函数里留下指向最后一个 span 的循环变量——留下了
       就等于还攥着一份不放，那一份的亏空落在量差值之后，补不回来（实测残余从每页约 1.2 次
       降到 0.12 次就是这一处的差别）。
    2. **必须走 `ctypes.Py_IncRef`，不能"拿个列表装一堆 `None`"**。那样装进去的引用会在解释器
       退出、模块全局被清理时统统还回去，等于把亏空原样重演一遍，崩溃只是从运行中挪到了退出时
       （实测就是这个表现）。`Py_IncRef` 加上去的引用不属于任何容器，永远不会被还回去，这正是
       这里需要的——`None` 本来就不该被释放。

    量到的差值不为正时不补，所以上游哪天修好了，这里自然停止补偿，不会反过来把引用计数推高。
    """
    before = sys.getrefcount(None)
    spans = page.get_texttrace()
    origins = {
        (round(ch[2][0], 2), round(ch[2][1], 2))
        for span in spans
        for ch in span["chars"]
        if chr(ch[0]) == "�"
    }
    del spans
    leaked = before - sys.getrefcount(None)
    none_obj = ctypes.py_object(None)
    for _ in range(leaked):
        ctypes.pythonapi.Py_IncRef(none_obj)
    return origins


def fabricated_char_origins(page) -> set[tuple[float, float]]:
    """这一页上"字形其实没有可靠Unicode、`get_text` 却编出了一个字"的位置（字符原点坐标）。

    调用方拿 `rawdict` 里每个字符的 `origin` 到这个集合里查，命中就是被编造的字。
    """
    return _texttrace_fabricated_origins(page)


def align_ocr_to_text(text: str, ocr: str) -> dict[int, str]:
    """文字层字符下标 -> OCR 给出的对应字符。

    **只认 1:1 的对应**（`equal` 段，以及两侧等长的 `replace` 段）：伪造位置在文字层是一个字符，
    OCR 那边也该是一个字符，长度不等就说明这一段根本没对齐上，宁可判读不出。
    """
    mapping: dict[int, str] = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, text, ocr, autojunk=False).get_opcodes():
        if tag == "equal" or (tag == "replace" and (i2 - i1) == (j2 - j1)):
            for k in range(i2 - i1):
                mapping[i1 + k] = ocr[j1 + k]
    return mapping


def line_agreement(text: str, mapping: dict[int, str], bad_idx: set[int]) -> float:
    """该行**非伪造位置**上，OCR 与文字层对上了多少（闸门用）。没有可比对的字符时算 1.0。"""
    others = [i for i in range(len(text)) if i not in bad_idx]
    if not others:
        return 1.0
    return sum(1 for i in others if mapping.get(i) == text[i]) / len(others)


def repair_line_chars(chars: list[dict], bad_idx: set[int], ocr_text: str) -> dict[int, str]:
    """给出伪造位置到"读出来的字"的映射；没过闸门或对不上的位置不出现在结果里。

    `chars` 是该行的字符字典列表（`rawdict` 的 `chars`），`bad_idx` 是其中伪造位置的下标。

    比较用文本要按生产口径剔掉占位码位/装饰符号（它们随后本来就会被删，OCR 那边也不会有
    对应输出，留着纯属制造"不一致"），因此下标会错位——用 `pos_of` 把 `chars` 的原下标映射
    到比较用文本里的位置，回填时再映射回去。伪造位置本身一定不在剔除之列（C0/PUA 不算伪造）。
    """
    if not ocr_text or not bad_idx:
        return {}
    kept = [(i, normalize_cjk_variants(c["c"])) for i, c in enumerate(chars) if not _is_out_of_scope(c["c"])]
    text = "".join(ch for _, ch in kept)
    pos_of = {orig: pos for pos, (orig, _) in enumerate(kept)}
    bad_pos = {pos_of[i] for i in bad_idx if i in pos_of}
    if not bad_pos:
        return {}
    mapping = align_ocr_to_text(text, ocr_text)
    if line_agreement(text, mapping, bad_pos) < config.NATIVE_GLYPH_REPAIR_MIN_LINE_AGREEMENT:
        return {}
    return {i: mapping[pos_of[i]] for i in bad_idx if i in pos_of and pos_of[i] in mapping}


def ocr_line_text(page_image, bbox: tuple[float, float, float, float], dpi: int) -> str:
    """把整页渲染图上的一行裁出来送识别，返回按 x 排序拼接的文本。

    传入的是**整页已渲染好的图**：一页往往有好几行要认，每行各渲染一次会把同一页反复光栅化。
    """
    zoom = dpi / 72
    pad = 2  # 四周留白，避免笔画贴边被切
    box = (
        max(0, int(bbox[0] * zoom) - pad),
        max(0, int(bbox[1] * zoom) - pad),
        min(page_image.width, int(bbox[2] * zoom) + pad),
        min(page_image.height, int(bbox[3] * zoom) + pad),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return ""
    # **取管线也要在 try 里**：没装 paddlepaddle、模型权重拉不下来、显存不足都在这一步炸，
    # 而它们和"这一行认不出来"是同一类情况——本模块有完好的降级路径（落记号→整句不送审），
    # 不该让一份普通的有文字层PDF因为缺OCR依赖就整篇解析失败。
    # 必须用 ocr_pdf._get_ocr_pipeline() 这种模块属性访问，不能 from ... import 那个名字——
    # 测试用 monkeypatch.setattr(ocr_pdf, "_get_ocr_pipeline", ...) 打桩，名字被 import 进
    # 本模块命名空间就打不中（core/parser/CLAUDE.md 记过这个坑）。
    try:
        from core.parser import ocr_pdf  # 延迟import：只有真检出伪造字符的文档才会碰OCR依赖
        import numpy as np

        pipeline = ocr_pdf._get_ocr_pipeline()
        result = list(pipeline.predict(np.asarray(page_image.crop(box))))[0]
    except Exception as exc:
        # 降级是静默的（正文只多几个记号），但缺依赖这种全局性失败必须在日志里留痕，
        # 否则表现成"还原率莫名很低"，无从查起。
        logger.warning("字形还原取OCR失败，本行按读不出处理：%s: %s", type(exc).__name__, exc)
        return ""
    texts = list(result.get("rec_texts", []))
    boxes = list(result.get("rec_boxes", []))
    if len(texts) != len(boxes):
        return "".join(texts)
    order = sorted(range(len(texts)), key=lambda i: float(boxes[i][0]))
    return "".join(texts[i] for i in order)
