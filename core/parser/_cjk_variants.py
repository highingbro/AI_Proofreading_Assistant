"""康熙部首/CJK兼容字形类"字形变体字符"在源头（A类原生PDF通道）归一化。

**根因**：部分PDF的字体ToUnicode CMap有缺陷，PyMuPDF按坐标提取出的正文汉字会落在
Unicode"CJK部首补充"/"康熙部首"/"CJK兼容汉字"这几个区块的码位上——肉眼看和标准汉字
毫无区别，但码位不同。真实文档（`预览版 8-9月 电子版-2026e-works活动计划`）统计过，
一份19页的刊物里有2122次这类字符，占了字符总量不小的比例。

在此之前，这类字符只在 `core/classifier/postprocess.py` 里被**下游、反应式**地处理——
判断"LLM建议改写的内容和原文相比是不是只差这类变体"，命中就丢弃或降级。这只能拦住
"LLM把变体字符本身当错别字报出来"这一种具体表现形式；LLM看到满屏这类陌生码位还会
产生别的困惑（比如把原文本来就重复出现的词误判成"排版错乱要去重"），每冒出一种新的
困惑表现形式就要在分类器那边再堵一个洞，属于治标不治本。**本模块改在解析阶段——LLM
和后续所有逻辑压根不会再看到这些变体字符**，从根上消掉整类困惑，而不是逐个堵表现形式。

## 归一化范围：只做"目标字符可确定"的安全归一化，不猜

四个区块里，只有两类能在不引入内容篡改风险的前提下安全改写：

1. **康熙部首**（U+2F00~2FD5）：绝大多数字符有Unicode官方NFKC兼容分解，直接给出对应
   汉字（如 `⼯`→`工`）；个别分解结果是繁体字形（这一区块本来就是"传统部首"），简体
   正文里代表的其实是简体字（如 `⼾`分解成`戶`，正文本意是`户`），靠 `_TRADITIONAL_FOLD`
   折回——这张表和 `core/classifier/postprocess.py::_KANGXI_TRADITIONAL_FOLDINGS`
   记录的是同一个Unicode事实，两处独立维护是有意的：分类器那边是"反应式兜底"，覆盖
   本模块没有归一化到的场景（比如本模块以外的场景、未来发现的新变体区块），职责不同，
   不应该因为共享几行数据就产生模块间的强耦合。
2. **CJK兼容汉字 + 兼容汉字补充**（U+F900~FAFF、U+2F800~2FA1D）：定义上就是"和某个
   规范汉字规范等价"的兼容区块，NFKC分解100%可靠，程序化算出整个区块的映射表，不需要
   人工核对，也不会有誊写错误。

**"CJK部首补充"区块（U+2E80~2EF3）故意只归一化其中一小部分，不做整块处理**：这个区块
完全没有NFKC分解（Unicode没把它当"规范汉字的兼容变体"看待，而是"字典部首索引用的
纯部首形式"），没有官方数据能程序化推导目标字符，必须人工判断。这个区块里大多数字符
（如"人字旁⺅""水字旁⺡""心字底⺗"）只在汉字内部当偏旁部首用，从未独立成字，如果真的
在文档里被单独提取出来，多半是别的解析问题而不是"这个部首代表某个规范汉字"，贸然指定
一个目标字符去替换属于臆测，有把内容改错的风险——这正是早期"部首→汉字"映射表被弃用的
同一个教训（见 postprocess.py 顶部），这里不重蹈覆辙。`_RADICAL_SUPPLEMENT_FOLD` 里
只收录了真实文档验证过的、Unicode官方字符名明确写出"C-SIMPLIFIED/J-SIMPLIFIED <某个
独立汉字>"（如`⻔`=`CJK RADICAL C-SIMPLIFIED GATE`→`门`）、且该字符本身就是常见独立
汉字（不是纯偏旁部首）的条目——边界依据是Unicode官方命名给出的语义，不是凭字形目测。
新文档若冒出这个区块里表里没有的字符，保留原样不动，交给分类器那层的反应式防线兜底
（`_is_cjk_variant_char` 对"无法归一化的变体字符"本就按"该区字符本就是某汉字的部首
形式"放行，不会因为本模块没收录就产生新的误伤）。

测试见 `tests/test_parser.py`"CJK变体字符源头归一化"一节；改动验证方式见
`core/parser/CLAUDE.md`"字形变体字符在源头归一化"一节（真实文档2122次出现里
2021次被本模块直接消除，占95.2%）。
"""

from __future__ import annotations

import unicodedata

# 康熙部首本来就是"传统"部首，NFKC分解结果一律是繁体字形；简体正文里这些字符代表的是
# 简体字（`⼾`分解成`戶`，正文其实是`户`）。跟 core/classifier/postprocess.py 里同名表
# 记录的是同一份Unicode事实，两处各自独立维护，理由见本文件顶部 docstring。
_TRADITIONAL_FOLD = {
    "戶": "户", "門": "门", "馬": "马", "車": "车", "頁": "页", "見": "见",
    "貝": "贝", "風": "风", "長": "长", "齒": "齿", "龜": "龟", "黽": "黾",
    "麥": "麦", "黃": "黄", "韋": "韦", "飛": "飞", "魚": "鱼", "鳥": "鸟",
    "龍": "龙", "齊": "齐", "鹵": "卤", "語": "语", "貞": "贞", "隸": "隶",
}

# CJK部首补充区块里，仅收录"Unicode官方字符名明确指向某个常见独立汉字"且真实文档验证
# 过会出现的条目（见本文件顶部docstring"归一化范围"一节的收录标准），键取自官方字符名
# 逐一核对：GATE=门 CART=车 LONG=长 WIND=风 HORN=角 DRAGON=龙 LEAF=页 YELLOW=黄
# FLY=飞 HORSE=马 SEE=见 WHEAT=麦 BONE=骨 TANNED LEATHER=韦。
_RADICAL_SUPPLEMENT_FOLD = {
    "⻔": "门",  # CJK RADICAL C-SIMPLIFIED GATE
    "⻋": "车",  # CJK RADICAL C-SIMPLIFIED CART
    "⻓": "长",  # CJK RADICAL C-SIMPLIFIED LONG
    "⻛": "风",  # CJK RADICAL C-SIMPLIFIED WIND
    "⻆": "角",  # CJK RADICAL SIMPLIFIED HORN
    "⻰": "龙",  # CJK RADICAL C-SIMPLIFIED DRAGON
    "⻚": "页",  # CJK RADICAL C-SIMPLIFIED LEAF
    "⻩": "黄",  # CJK RADICAL SIMPLIFIED YELLOW
    "⻜": "飞",  # CJK RADICAL C-SIMPLIFIED FLY
    "⻢": "马",  # CJK RADICAL C-SIMPLIFIED HORSE
    "⻅": "见",  # CJK RADICAL C-SIMPLIFIED SEE
    "⻨": "麦",  # CJK RADICAL SIMPLIFIED WHEAT
    "⻣": "骨",  # CJK RADICAL BONE
    "⻙": "韦",  # CJK RADICAL C-SIMPLIFIED TANNED LEATHER
}


def _build_nfkc_variant_map(lo: int, hi: int) -> dict[str, str]:
    """程序化算出 [lo, hi] 区块内、NFKC分解结果与自身不同的码位映射表。

    不手工誊写——这几个区块（康熙部首、CJK兼容汉字及其补充）的NFKC分解本身就是
    Unicode标准数据，`unicodedata` 模块直接查询即可，程序算比人工抄表更不容易出错。
    """
    result: dict[str, str] = {}
    for cp in range(lo, hi + 1):
        ch = chr(cp)
        try:
            unicodedata.name(ch)
        except ValueError:
            continue  # 未分配码位，跳过
        nfkc = unicodedata.normalize("NFKC", ch)
        if nfkc != ch:
            result[ch] = _TRADITIONAL_FOLD.get(nfkc, nfkc)
    return result


# 三段来源合并成一张扁平映射表：康熙部首(NFKC+繁体折回)、CJK兼容汉字、CJK兼容汉字补充
# （三者都有NFKC官方分解，程序化生成）+ CJK部首补充里人工核对过的安全子集。
_VARIANT_NORMALIZE_MAP: dict[str, str] = {
    **_build_nfkc_variant_map(0x2F00, 0x2FD5),
    **_build_nfkc_variant_map(0xF900, 0xFAFF),
    **_build_nfkc_variant_map(0x2F800, 0x2FA1D),
    **_RADICAL_SUPPLEMENT_FOLD,
}


def normalize_cjk_variants(text: str) -> str:
    """把 text 里已知安全的CJK变体字符替换成对应规范汉字，其余字符原样保留。

    在 `native_pdf.py::_extract_native_page_raw` 拼出block文本后立即调用——越早
    归一化，后续分块/校对/分层看到的都是干净文本，不需要逐个环节各自防御。
    """
    if not any(ch in _VARIANT_NORMALIZE_MAP for ch in text):
        return text
    return "".join(_VARIANT_NORMALIZE_MAP.get(ch, ch) for ch in text)
