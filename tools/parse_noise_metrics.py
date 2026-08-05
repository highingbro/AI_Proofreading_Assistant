"""量化 A 类 PDF 解析残留的三类噪声，改动解析逻辑前后各跑一次做验收。

**为什么要有这个脚本**：解析层的改动此前只用"全文本 diff 无回归 / 单元测试全绿 / 分栏验收线
不变"验收，这三条都看不见"最终送进 LLM 的文本还有多少伪影"。真实教训是分栏检测改对之后，
栏宽变窄、被栏宽顶回来的换行大量增加，数据库里一次校对的问题数从 54 涨到 108，而前述三条
验收全是绿的。这里的三个数就是那三类噪声的可机械计数的代理指标，零 API 成本、确定性可复算。

三个指标（都是"越小越好"，但**只准改动针对的那个降，另外两个不许变坏**）：

- **句中换行**：把一页里的块按阅读顺序用 `\\n` 拼起来（`core/chunker/` 就是这么做的，见
  core/chunker/CLAUDE.md），数其中"上一个字符不是句末标点"的换行——LLM 看到的词中断行。
- **位置串含换行**：`source_location` 里带 `\\n` 的块数，UI 和 Excel 里会断成两行显示。
- **括号错位残留**：`《` 后面直接跟空白、或 `数字 《 .` 这种形状，是全角开括号墨迹偏移导致的
  字序错乱（见 core/parser/_glyphs.py）。

只跑 A 类（有文字层）PDF：`ocr="off"` 时无文字层的页会抛 `NoTextLayerError`，整份跳过。
B 类走 PaddleOCR，是另一套装配逻辑，不在本脚本的量化范围内。

用法：

    python -X utf8 tools/parse_noise_metrics.py                   # 全部样本，打印三个数
    python -X utf8 tools/parse_noise_metrics.py --dump out/before # 顺带把全文本 dump 出来做 diff
    python -X utf8 tools/parse_noise_metrics.py 预览版             # 只跑文件名含该子串的样本
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core.parser import parse_document  # noqa: E402
from core.parser._types import NoTextLayerError  # noqa: E402

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"

# 与 _paragraphs.py 的口径一致：这些字符收尾的换行是"内容说完了主动换的"，不算噪声。
_STOP_CHARS = "。！？；…：.!?;:）)》」』】〕》\"'”’"

# `《` 后紧跟空白 → 开括号被排到了它后面那个字符的前面；`1 《 .` → 序号点被吞进括号里。
_BRACKET_MISORDER_RES = (re.compile(r"《\s"), re.compile(r"\d\s*《\s*\."))


def _glyph_metrics(blocks) -> tuple[int, int]:
    """字形读不出的字符数，以及因此被排除送审的正文字数。

    本脚本一律用 `ocr='off'` 调 `parse_document`，所以量到的是**最坏路径**：字形还原全部
    不生效、检出的伪造字符统统落成记号。这个上界正是我们要盯的——它说明"万一还原完全
    失效，盲区有多大"。真实路径（还原开着）的残留由 `tools/glyph_repair_report.py` 量。
    """
    from core.chunker.fill_units import _drop_unreadable_clauses

    marks = sum(b.text.count(config.NATIVE_UNREADABLE_GLYPH_MARK) for b in blocks)
    dropped = sum(len(b.text) - len(_drop_unreadable_clauses(b.text)) for b in blocks)
    return marks, dropped


def _metrics(blocks) -> tuple[int, int, int]:
    mid_sentence_breaks = bracket_misorder = location_breaks = 0
    prev_tail = ""
    for b in blocks:
        # 块与块之间那个换行也要算——真实噪声的绝大多数就在这里，不在块内部
        if prev_tail and prev_tail[-1] not in _STOP_CHARS:
            mid_sentence_breaks += 1
        for i, ch in enumerate(b.text):
            if ch == "\n" and i and b.text[i - 1] not in _STOP_CHARS:
                mid_sentence_breaks += 1
        prev_tail = b.text
        if any(r.search(b.text) for r in _BRACKET_MISORDER_RES):
            bracket_misorder += 1
        if "\n" in (b.source_location or ""):
            location_breaks += 1
    return mid_sentence_breaks, location_breaks, bracket_misorder


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("needle", nargs="?", help="只跑文件名含该子串的样本")
    ap.add_argument("--dump", metavar="DIR", help="把每份文档的全部块文本逐行写到该目录，供改动前后 diff")
    args = ap.parse_args()

    dump_dir = Path(args.dump) if args.dump else None
    if dump_dir:
        dump_dir.mkdir(parents=True, exist_ok=True)

    paths = sorted(SAMPLES_DIR.glob("*.pdf"))
    if args.needle:
        paths = [p for p in paths if args.needle in p.name]

    totals = [0, 0, 0, 0, 0]
    print("%-42s %8s %8s %8s %8s %8s %7s"
          % ("文档", "句中换行", "位置断行", "括号错位", "读不出字", "弃审字数", "块数"))
    for path in paths:
        try:
            doc = parse_document(path, ocr="off")
        except NoTextLayerError as exc:
            print("%-42s  跳过（%s）" % (path.name[:42], type(exc).__name__))
            continue
        m = _metrics(doc.blocks) + _glyph_metrics(doc.blocks)
        totals = [t + v for t, v in zip(totals, m)]
        print("%-42s %8d %8d %8d %8d %8d %7d"
              % (path.name[:42], m[0], m[1], m[2], m[3], m[4], len(doc.blocks)))
        if dump_dir:
            # 位置串也要 repr：它本身可能含换行（正是本脚本要量的噪声之一），
            # 直接写进去会把一条记录劈成两行，diff 的行号全错位。
            lines = [f"{b.source_location!r}\t{b.text!r}" for b in doc.blocks]
            (dump_dir / (path.stem + ".txt")).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("%-42s %8d %8d %8d %8d %8d" % ("合计", *totals))


if __name__ == "__main__":
    main()
