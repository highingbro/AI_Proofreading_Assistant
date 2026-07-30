"""A类分栏检测新旧算法并排对比：改分栏逻辑后的验收关卡。

`tools/probe_columns.py` 回答"新算法在这份文档上判几栏"；本脚本回答**换算法之后送进
LLM的文本到底变了什么**——这才是这次重写要治的病：栏数判错以后，`_order_native_page`
把不同栏的块按 y 坐标交错排列，`core/chunker/` 在块接缝处把不相邻的文字并排放到一起，
LLM 对着原文根本不存在的"词"报错别字。所以光比栏数不够，必须把两套边界分别喂给真正的
阅读顺序还原逻辑，逐页比对**块接缝处的相邻字对**。

新文档要换/要调分栏阈值时跑这个，不要只跑单元测试——单测是构造的理想几何（对称四栏、
通栏标题），覆盖不了真实版面的意外情况。

三块输出：

1. **逐页栏数对照**：旧 / 新 / 人工核定GT（GT表在 `probe_columns.py::GROUND_TRUTH`），
   标出 收益(旧错新对) / ⛔回归(旧对新错) / 都错。
2. **差异页的接缝字对 diff**：消失的接缝字对（旧算法粘出来的假词，修好了）与**新增的
   接缝字对**（新算法自己粘出来的，是真正要盯的东西）。附完整阅读顺序文本的 difflib。
3. **文档级 layout_mode 新旧值**：防 `_majority_layout_mode` 因更多页判 double 而翻成
   `mixed`（会连带改变全文档的 `_source_location` 表述，属于隐性回归）。

**验收线：⛔回归 = 0，且四栏收益 ≥ 5 页。** 有一页回归就回去重调阈值，不放行。

用法：

    # 全部标定样本（GT 覆盖的6份），只看汇总与验收线
    python -X utf8 tools/compare_columns.py --all

    # 单份文档 + 差异页的接缝字对与文本 diff
    python -X utf8 tools/compare_columns.py "samples/数字化企业期刊82期V6.25.pdf" --seams

    # 连完整阅读顺序文本的 difflib 一起打（很长，排查单页时才用）
    python -X utf8 tools/compare_columns.py <pdf> --pages 11 --seams --text-diff
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.parser import _majority_layout_mode                      # noqa: E402
from core.parser import native_pdf                                 # noqa: E402
from core.parser._columns import _detect_column_boundaries as NEW   # noqa: E402
# GT 表、A类取块、切换前算法的冻结副本都只在 probe_columns.py 里维护一份，避免两处对不上
# （tools/ 不是包，所以把 tools/ 也加进 sys.path 按顶层模块导）
from probe_columns import (  # noqa: E402
    _detect_column_boundaries_legacy as OLD,
    _gt_for,
    load_native_pages,
)

# 标定样本：3份多栏期刊（收益来源）+ 3份单栏文档（误判守门）。
# GT 表本身在 probe_columns.py 里，这里只列文件名，避免两处各存一份对不上。
SAMPLE_FILES = [
    "预览版 8-9月 电子版-2026e-works活动计划-7月更新v1.pdf",
    "数字化企业期刊79期-终版.pdf",
    "数字化企业期刊82期V6.25.pdf",
    "sample.pdf",
    "武昌首义学院高端装备智能运维现代产业学院在线学习平台_用户使用手册（学生版）V1.0__学生_2026.7.13.pdf",
    "武昌首义学院高端装备智能运维现代产业学院在线学习平台_后台管理系统使用手册V1.0__后台管理员_2026.7.13.pdf",
]

MIN_FOUR_COLUMN_GAIN = 5      # 验收线：四栏收益页数下限


def _order_with(
    boundaries: list[float], blocks: list[dict], width: float
) -> tuple[list[dict], str]:
    """拿一组指定的分栏边界跑真正的 `_order_native_page`，返回排好序的块。

    直接打桩 `native_pdf._detect_column_boundaries`（`native_pdf.py` 是
    `from ... import` 进来的，名字在它自己的模块命名空间里）而不是复制一份排序逻辑：
    这个工具的全部意义就是"生产的排序逻辑在两套边界下分别会输出什么"，复制一份就等于
    在验证复制品，排序逻辑将来一改，对比结论立刻失真。
    """
    original = native_pdf._detect_column_boundaries
    native_pdf._detect_column_boundaries = lambda _b, _w: boundaries
    try:
        # 传 blocks 的浅拷贝：_order_native_page 会往块字典里写 column 字段，两轮之间
        # 不隔开的话后一轮会看到前一轮的残留（这里只读 text/bbox，但别给后人留坑）。
        ordered, mode = native_pdf._order_native_page(
            [dict(b) for b in blocks], width, force_layout="auto"
        )
    finally:
        native_pdf._detect_column_boundaries = original
    return ordered, mode


def _seam_pairs(ordered: list[dict]) -> list[str]:
    """阅读顺序里每个块接缝处的"末字+首字"字对。

    这就是病灶的最小可观测单位：`core/chunker/` 在块之间插换行送审，接缝两侧的字在
    LLM 眼里就是跨行相邻的一对字。栏数判错时这里会出现"规|与"这种原文不存在的组合，
    LLM 据此报"应改为『规划与』"。
    """
    texts = [b["text"].strip() for b in ordered if b["text"].strip()]
    return [f"{a[-1]}|{b[0]}" for a, b in zip(texts, texts[1:])]


def _reading_text(ordered: list[dict]) -> str:
    """按 `core/chunker/` 的做法用换行拼接，作为 difflib 的输入。"""
    return "\n".join(b["text"].strip() for b in ordered if b["text"].strip())


def compare_document(path: Path, wanted_pages: list[int] | None) -> dict:
    pages = load_native_pages(path)
    gt = _gt_for(path)
    rows, old_modes, new_modes = [], [], []

    for i, (blocks, width) in enumerate(pages):
        if wanted_pages is not None and i not in wanted_pages:
            continue
        old_lines = OLD(blocks, width) if blocks else []
        new_lines = NEW(blocks, width) if blocks else []
        old_ordered, old_mode = _order_with(old_lines, blocks, width)
        new_ordered, new_mode = _order_with(new_lines, blocks, width)
        old_modes.append(old_mode)
        new_modes.append(new_mode)
        rows.append({
            "page": i,
            "old_lines": old_lines,
            "new_lines": new_lines,
            "old_n": len(old_lines) + 1,
            "new_n": len(new_lines) + 1,
            "gt": gt.get(i),
            "old_seams": _seam_pairs(old_ordered),
            "new_seams": _seam_pairs(new_ordered),
            "old_text": _reading_text(old_ordered),
            "new_text": _reading_text(new_ordered),
        })

    return {
        "path": path,
        "rows": rows,
        "old_layout_mode": _majority_layout_mode(old_modes),
        "new_layout_mode": _majority_layout_mode(new_modes),
    }


def _print_document(result: dict, show_seams: bool, show_text_diff: bool) -> None:
    rows = result["rows"]
    print("\n" + "=" * 100)
    print(f"{result['path'].name}   共 {len(rows)} 页")
    print(f"  文档级 layout_mode: 旧={result['old_layout_mode']}  新={result['new_layout_mode']}"
          + ("   <<< 变了，注意 _source_location 表述会跟着变"
             if result["old_layout_mode"] != result["new_layout_mode"] else ""))

    print(f"\n  {'页':>4} {'旧':>4} {'新':>4} {'GT':>4}  判定")
    for r in rows:
        verdict = ""
        if r["gt"] is not None:
            if r["new_n"] == r["gt"] and r["old_n"] != r["gt"]:
                verdict = "收益"
            elif r["old_n"] == r["gt"] and r["new_n"] != r["gt"]:
                verdict = "⛔回归"
            elif r["new_n"] != r["gt"]:
                verdict = "都错"
        changed = "" if r["old_n"] == r["new_n"] else "  栏数变了"
        gt_s = str(r["gt"]) if r["gt"] is not None else "-"
        if verdict or changed:
            print(f"  {r['page']:>4} {r['old_n']:>4} {r['new_n']:>4} {gt_s:>4}  {verdict}{changed}")

    if not show_seams:
        return

    for r in rows:
        if r["old_seams"] == r["new_seams"]:
            continue
        gone = [s for s in r["old_seams"] if s not in r["new_seams"]]
        added = [s for s in r["new_seams"] if s not in r["old_seams"]]
        print(f"\n  --- 第{r['page']}页接缝字对（旧{r['old_n']}栏 → 新{r['new_n']}栏）---")
        print(f"    旧边界={[round(x, 1) for x in r['old_lines']]}"
              f"  新边界={[round(x, 1) for x in r['new_lines']]}")
        print(f"    消失的接缝({len(gone)}): {' '.join(gone[:30])}")
        print(f"    新增的接缝({len(added)}): {' '.join(added[:30])}")
        if show_text_diff:
            diff = difflib.unified_diff(
                r["old_text"].splitlines(), r["new_text"].splitlines(),
                fromfile="旧阅读顺序", tofile="新阅读顺序", lineterm="", n=1,
            )
            print("\n".join(f"    {line}" for line in diff))


def _brief(items: list[tuple[str, dict]]) -> str:
    """把有问题的页压成一行，方便直接贴进记录。"""
    if not items:
        return ""
    return " ".join(
        f"{name[:8]}p{r['page']}(旧{r['old_n']}新{r['new_n']}GT{r['gt']})" for name, r in items
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="A类分栏检测新旧对比（页号 0-indexed）")
    ap.add_argument("file_path", nargs="?", help="不给则需要 --all")
    ap.add_argument("--all", action="store_true", help="跑全部标定样本并给出验收结论")
    ap.add_argument("--pages", help="只看这些页，如 8-15 或 3,7,11")
    ap.add_argument("--seams", action="store_true", help="打印差异页的接缝字对变化")
    ap.add_argument("--text-diff", action="store_true",
                    help="连完整阅读顺序文本的 difflib 一起打（很长，配合 --pages 用）")
    args = ap.parse_args()

    if not args.all and not args.file_path:
        ap.error("给一个文件路径，或用 --all 跑全部标定样本")

    wanted: list[int] | None = None
    if args.pages:
        wanted = []
        for part in args.pages.split(","):
            if "-" in part:
                a, b = part.split("-", 1)
                wanted.extend(range(int(a), int(b) + 1))
            else:
                wanted.append(int(part))

    root = Path(__file__).resolve().parent.parent
    if args.all:
        paths = [root / "samples" / name for name in SAMPLE_FILES]
        missing = [p for p in paths if not p.exists()]
        if missing:
            print(f"缺样本文件，无法给出验收结论：{[p.name for p in missing]}")
            return
    else:
        paths = [Path(args.file_path)]

    all_rows: list[tuple[str, dict]] = []
    mode_changes: list[str] = []
    for path in paths:
        result = compare_document(path, wanted)
        _print_document(result, args.seams, args.text_diff)
        all_rows.extend((path.name, r) for r in result["rows"])
        if result["old_layout_mode"] != result["new_layout_mode"]:
            mode_changes.append(
                f"{path.name}: {result['old_layout_mode']}→{result['new_layout_mode']}"
            )

    scored = [(n, r) for n, r in all_rows if r["gt"] is not None]
    gains = [(n, r) for n, r in scored if r["new_n"] == r["gt"] != r["old_n"]]
    regressions = [(n, r) for n, r in scored if r["old_n"] == r["gt"] != r["new_n"]]
    both_bad = [(n, r) for n, r in scored if r["new_n"] != r["gt"] != r["old_n"]
                and r["old_n"] != r["gt"]]
    four_gains = [(n, r) for n, r in gains if r["gt"] == 4]
    seam_changed = [(n, r) for n, r in all_rows if r["old_seams"] != r["new_seams"]]

    print("\n" + "=" * 100)
    print("汇总")
    print(f"  有GT的页: {len(scored)}   旧算法对={sum(r['old_n'] == r['gt'] for _, r in scored)}"
          f"   新算法对={sum(r['new_n'] == r['gt'] for _, r in scored)}")
    print(f"  收益(旧错新对)={len(gains)}  其中四栏收益={len(four_gains)}")
    print(f"  ⛔回归(旧对新错)={len(regressions)}  {_brief(regressions)}")
    print(f"  都错={len(both_bad)}  {_brief(both_bad)}")
    print(f"  阅读顺序接缝有变化的页: {len(seam_changed)} / {len(all_rows)}")
    print(f"  文档级 layout_mode 变化: {mode_changes or '无'}")

    if not args.all:
        print("\n（只跑了部分样本，不给验收结论；用 --all 跑全部）")
        return

    print("\n验收线检查")
    ok_regression = len(regressions) == 0
    ok_gain = len(four_gains) >= MIN_FOUR_COLUMN_GAIN
    print(f"  回归页数 = 0 ................ {'通过' if ok_regression else '未通过'}"
          f"（实际 {len(regressions)}）")
    print(f"  四栏收益 >= {MIN_FOUR_COLUMN_GAIN} 页 ............. "
          f"{'通过' if ok_gain else '未通过'}（实际 {len(four_gains)}）")
    print(f"\n结论：{'可以放行' if ok_regression and ok_gain else '不放行，回去重调阈值'}")
    sys.exit(0 if ok_regression and ok_gain else 1)


if __name__ == "__main__":
    main()
