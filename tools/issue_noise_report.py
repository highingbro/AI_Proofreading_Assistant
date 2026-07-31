"""按噪声分类对比两条校对记录的 issue，验收解析层改动到底减没减少假错误。

**为什么不看总条数**：同一份文档同一模式连跑五次（记录46~50），总条数在 35/71/83/54 之间
波动——LLM 每次报什么本来就有随机性，单次总数没有判别力。下面三类判据是机械可判的、口径
写死在脚本里，每次复算完全一致，稳定得多。

| 分类 | 判据 | 是什么 |
|---|---|---|
| N1 括号错位 | `original_text` 匹配 `《\\s` 或 `\\d\\s*《\\s*\\.` | 全角开括号墨迹偏移导致的字序错乱 |
| N2 位置串断裂 | `page_location` 含 `\\n` | 跨页对开版面一页印两个页码，位置描述断成两行 |
| N3 换行造成的假错误 | `original_text` 含 `\\n`，且这条 issue 说的就是那个换行 | LLM 把换行当成多余空格/漏字/排版错误报 |

N3 的判法要解释一下：这类 issue 的建议本质是"把 `A\\nB` 改成 `AB`"，所以只看形式特征、不理解
语义（两条支路见 `_is_n3` 的 docstring）。

**N3 必须拆成两栏看，合计数会骗人**：

- **N3长**（`original_text` ≥ `_N3_LONG_CHARS` 字）＝正文长段被栏宽切开，这是 `_paragraphs.py`
  的块级合并针对的那一类。
- **N3短**＝表格/日历单元格里的两行标题（`APS高级智能计划与` / `排产原理实训班`）。这类的第一行
  **没有顶到栏最右**（单元格比栏窄），块级合并的"满行"闸门会正确地放过它——它是另一件事，
  要治得先有单元格概念。

实测教训：步骤1 改完后 N3 合计从 35 涨到 53，看着像变坏了，拆开才看到 N3长 27→8、涨的全是
一页活动日历上的 N3短（那一页的块**改动前后逐字节相同**）。单次跑哪些页会被 LLM 逐条列出来
本身就是随机的，所以永远看分栏、并且核对"变化的那一栏对应的块文本到底变没变"。

用法：

    python -X utf8 tools/issue_noise_report.py 53 54      # 基线记录 vs 新记录
    python -X utf8 tools/issue_noise_report.py 53 54 -v   # 顺带列出每类的具体条目
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

_N1_RES = (re.compile(r"《\s"), re.compile(r"\d\s*《\s*\."))
# 建议里能对应上原文的最短片段长度。太短（2~3字）会被无关的高频词碰巧命中。
_N3_MIN_FRAGMENT = 4
# N3 长短分界：正文一行排满约 20~24 字，两行标题两截加起来普遍在 20 字以内，40 是干净的分界。
_N3_LONG_CHARS = 40


def _is_n3(original: str, suggestion: str) -> bool:
    """原文含换行，且这条 issue 说的就是那个换行本身。

    两条支路缺一不可，对应 LLM 的两种措辞习惯，都只看形式、不理解语义：

    - **建议里出现了"跨过换行才连得起来"的片段**——即某个片段在原文去掉换行后存在、在原文里
      不存在（`…应用场\\n景…` → 建议里写 `应用场景`）。
    - **建议把换行两侧分别引了一遍**（`建议将"APS高级智能计划与"与"排产原理实训班"合并为
      一行`）。第一条支路对这种措辞无效——两个半截被引号和"与"字隔开，拼不出跨换行的片段。
      漏掉它会严重低估：实测一份活动日历页上这一种就有 18 条。
    """
    if "\n" not in original:
        return False
    flat = original.replace("\n", "")
    text = suggestion or ""
    crossing = any(
        flat.find(text[i:i + _N3_MIN_FRAGMENT]) >= 0
        for i in range(len(text) - _N3_MIN_FRAGMENT + 1)
        if "\n" not in text[i:i + _N3_MIN_FRAGMENT]
        and text[i:i + _N3_MIN_FRAGMENT] not in original
    )
    if crossing:
        return True
    return any(
        len(head) >= _N3_MIN_FRAGMENT and len(tail) >= _N3_MIN_FRAGMENT
        and head[-_N3_MIN_FRAGMENT:] in text and tail[:_N3_MIN_FRAGMENT] in text
        for head, tail in ((original[:i], original[i + 1:]) for i, ch in enumerate(original) if ch == "\n")
    )


def _classify(rows) -> dict:
    buckets: dict = {"N1": [], "N2": [], "N3长": [], "N3短": [], "总数": rows}
    for r in rows:
        original, suggestion, location = r["original_text"] or "", r["suggestion"] or "", r["page_location"] or ""
        if any(p.search(original) for p in _N1_RES):
            buckets["N1"].append(r)
        if "\n" in location:
            buckets["N2"].append(r)
        if _is_n3(original, suggestion):
            buckets["N3长" if len(original) >= _N3_LONG_CHARS else "N3短"].append(r)
    return buckets


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("baseline", type=int, help="基线 record_id")
    ap.add_argument("current", type=int, help="待验收 record_id")
    ap.add_argument("-v", "--verbose", action="store_true", help="列出每类的具体条目")
    args = ap.parse_args()

    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    sides = {}
    for rid in (args.baseline, args.current):
        rec = conn.execute("SELECT doc_name, mode, created_at FROM records WHERE record_id=?", (rid,)).fetchone()
        if rec is None:
            raise SystemExit(f"记录 {rid} 不存在")
        rows = conn.execute("SELECT * FROM issues WHERE record_id=?", (rid,)).fetchall()
        sides[rid] = (rec, _classify(rows))
        print("记录%-4d %s  模式=%s  %s" % (rid, rec["created_at"][:16], rec["mode"], rec["doc_name"][-40:]))

    print()
    print("%-24s %10s %10s %8s" % ("分类", f"记录{args.baseline}", f"记录{args.current}", "变化"))
    for key, label in (("总数", "总条数（无判别力，仅参考）"), ("N1", "N1 括号错位"),
                       ("N2", "N2 位置串断裂"), ("N3长", "N3长 正文被栏宽切开"),
                       ("N3短", "N3短 单元格两行标题")):
        a, b = len(sides[args.baseline][1][key]), len(sides[args.current][1][key])
        print("%-24s %10d %10d %+8d" % (label, a, b, b - a))

    if args.verbose:
        for key in ("N1", "N2", "N3长", "N3短"):
            print(f"\n===== {key} @ 记录{args.current} =====")
            for r in sides[args.current][1][key]:
                print("  [%s] %r → %r" % (r["page_location"], (r["original_text"] or "")[:60],
                                          (r["suggestion"] or "")[:60]))


if __name__ == "__main__":
    main()
