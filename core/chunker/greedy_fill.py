"""单块正文装填：贪心装填 + heading 推块规则。"""

from __future__ import annotations

from core.chunker.fill_units import _FillUnit


def _fill_body(units: list[_FillUnit], start: int, target: int) -> tuple[list[_FillUnit], int]:
    """从 units[start:] 开始贪心装填一个块的正文，返回 (本块 units 列表, 下一个起始下标)。"""
    group: list[_FillUnit] = []
    total = 0
    i = start
    n = len(units)

    while i < n:
        unit = units[i]

        if unit.block_type == "heading" and group:
            # 试算：如果把这个 heading 也装进来，块会有多长
            projected = total + len(unit.text)
            next_unit = units[i + 1] if i + 1 < n else None
            # 判断"heading 之后紧跟的内容"是否装不进本块
            # （下一个单元不存在、或装不下都算溢出）
            next_would_overflow = (
                next_unit is None
                or projected + len(next_unit.text) > target
            )
            # heading 本身还能塞进本块，但塞进来后面紧跟的内容就装不下了
            # ——也就是 heading 会孤零零地卡在块尾，此时提前收尾，
            # 把 heading 让给下一块开头，避免"标题独占块尾"的割裂感
            if projected <= target and next_would_overflow:
                break

        # 常规贪心装填：试算加入当前单元后的总长度
        projected = total + len(unit.text)
        if group and projected > target:
            # 块内已有内容，且再装入会超过目标大小 —— 留给下一块
            # （若 group 为空则即使超 target 也强制装入，保证块非空推进）
            break

        group.append(unit)
        total += len(unit.text)
        i += 1

    return group, i
