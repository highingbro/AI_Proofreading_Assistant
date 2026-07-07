"""Excel 导出模块（阶段8实现）。

负责按页码排序、高优先级标红、引文类标色，导出校对/比对结果清单。
本阶段仅留函数签名。
"""

from pathlib import Path


def export_issues_to_excel(record_id: int) -> Path:
    """将指定流程记录下的问题导出为 Excel 文件，返回文件路径。"""
    raise NotImplementedError
