"""逐处报告"字形被字体映射成了另一个汉字"的检测与还原结果。

`tools/parse_noise_metrics.py` 用 `ocr='off'` 量的是最坏路径（还原全不生效时盲区多大），
本脚本走**真实路径**：真的渲染、真的调 OCR，看还原率、闸门各档保留数、以及最终有多少正文
因为读不出而被排除送审。耗 OCR 推理（不耗 LLM 额度），改 `core/parser/_glyph_repair.py`
前后各跑一次做验收。

用法（Windows 下务必用 PowerShell 且带 -X utf8，理由见 core/parser/CLAUDE.md 编码一节）：
    python -X utf8 tools/glyph_repair_report.py "samples/某文档.pdf"
    python -X utf8 tools/glyph_repair_report.py --all
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fitz  # noqa: E402

import config  # noqa: E402
from core.chunker.fill_units import _drop_unreadable_clauses  # noqa: E402
from core.parser import parse_document  # noqa: E402
from core.parser._glyph_repair import (  # noqa: E402
    _is_out_of_scope,
    align_ocr_to_text,
    fabricated_char_origins,
    line_agreement,
    ocr_line_text,
)
from core.parser._cjk_variants import normalize_cjk_variants  # noqa: E402
from core.parser._types import NoTextLayerError  # noqa: E402
from core.parser.ocr_pdf import _render_page_image  # noqa: E402

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"
MARK = config.NATIVE_UNREADABLE_GLYPH_MARK


def _rows_for(path: Path) -> list[dict]:
    """逐处收集：假字 / 还原成什么 / 上下文 / 本行一致率。

    这里重跑一遍检测与对齐（而不是读 parse_document 的产出），是为了拿到"一致率"这个
    中间量——它决定闸门取值，是本脚本存在的意义。判据与生产代码同源，改了要同步。
    """
    rows: list[dict] = []
    doc = fitz.open(path)
    for pno in range(doc.page_count):
        page = doc[pno]
        bad_origins = fabricated_char_origins(page)
        if not bad_origins:
            continue
        page_image = None
        for block in page.get_text("rawdict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                chars = [c for s in line["spans"] for c in s["chars"]]
                bad_idx = {
                    i for i, c in enumerate(chars)
                    if (round(c["origin"][0], 2), round(c["origin"][1], 2)) in bad_origins
                    and not _is_out_of_scope(c["c"])
                }
                if not bad_idx:
                    continue
                if page_image is None:
                    page_image = _render_page_image(page, config.NATIVE_GLYPH_REPAIR_DPI)
                bbox = (
                    min(c["bbox"][0] for c in chars), min(c["bbox"][1] for c in chars),
                    max(c["bbox"][2] for c in chars), max(c["bbox"][3] for c in chars),
                )
                ocr = ocr_line_text(page_image, bbox, config.NATIVE_GLYPH_REPAIR_DPI)
                kept = [(i, normalize_cjk_variants(c["c"])) for i, c in enumerate(chars)
                        if not _is_out_of_scope(c["c"])]
                text = "".join(ch for _, ch in kept)
                pos_of = {orig: pos for pos, (orig, _) in enumerate(kept)}
                mapping = align_ocr_to_text(text, ocr) if ocr else {}
                rate = line_agreement(text, mapping, {pos_of[i] for i in bad_idx if i in pos_of})
                for i in sorted(bad_idx):
                    p = pos_of.get(i)
                    lo, hi = max(0, (p or 0) - 6), min(len(text), (p or 0) + 7)
                    rows.append({
                        "page": pno + 1,
                        "fake": chars[i]["c"],
                        "got": mapping.get(p, "") if p is not None else "",
                        "context": text[lo:hi],
                        "rate": rate,
                    })
    doc.close()
    return rows


def _report(path: Path) -> None:
    rows = _rows_for(path)
    print("=" * 78)
    print(path.name)
    if not rows:
        print("  没有检出被编造的字形")
        return
    print("  %-4s %-4s %-4s %-30s %s" % ("页", "假字", "还原", "上下文", "本行一致率"))
    for r in rows:
        print("  %-4d %-4s %-4s %-30s %.2f"
              % (r["page"], r["fake"], r["got"] or "－", r["context"], r["rate"]))

    gate = config.NATIVE_GLYPH_REPAIR_MIN_LINE_AGREEMENT
    got = [r for r in rows if r["got"]]
    kept = [r for r in got if r["rate"] >= gate]
    print("\n  检出 %d 处；对齐上 %d 处（%.0f%%）；过闸门(>=%.2f) %d 处（%.0f%%）"
          % (len(rows), len(got), 100 * len(got) / len(rows), gate, len(kept),
             100 * len(kept) / len(rows)))
    print("  闸门各档保留数：", {
        "%.2f" % g: sum(1 for r in got if r["rate"] >= g) for g in (0.0, 0.5, 0.7, 0.8, 0.9, 1.0)
    })
    print("  一致率分布：", dict(sorted(collections.Counter("%.1f" % r["rate"] for r in got).items())))

    # 走生产路径实际解析一遍，看最终有多少正文因为读不出而进不了送审文本
    parsed = parse_document(path)
    total = sum(len(b.text) for b in parsed.blocks)
    dropped = sum(len(b.text) - len(_drop_unreadable_clauses(b.text)) for b in parsed.blocks)
    marks = sum(b.text.count(MARK) for b in parsed.blocks)
    print("  还原后残留记号 %d 处；因此被排除送审 %d / %d 字（%.2f%%）"
          % (marks, dropped, total, 100 * dropped / max(total, 1)))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file_path", nargs="?", help="不给则需要 --all")
    ap.add_argument("--all", action="store_true", help="跑 samples/ 下全部PDF")
    args = ap.parse_args()

    if args.all:
        paths = sorted(SAMPLES_DIR.glob("*.pdf"))
    elif args.file_path:
        paths = [Path(args.file_path)]
    else:
        ap.error("给一个文件路径，或用 --all")

    for path in paths:
        try:
            _report(path)
        except NoTextLayerError:
            print("=" * 78)
            print("%s\n  跳过（无文字层，走B类OCR通道，不存在这类伪影）" % path.name)


if __name__ == "__main__":
    main()
