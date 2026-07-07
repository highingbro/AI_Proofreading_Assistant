"""长文档分块模块（阶段3实现）。

负责按页/段落边界切割长文档，块间保留上下文重叠，
并记录每块的页码偏移，供问题定位回原文位置。本阶段仅留函数签名。
"""


def chunk_document(parsed_pages: list[dict]) -> list[dict]:
    """将解析后的文档按边界切块，返回带页码偏移的分块列表。"""
    raise NotImplementedError
