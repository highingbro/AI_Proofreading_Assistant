"""调试辅助脚本：串起 解析→分块→校对 全链路，跑真实LLM调用。

用法：
    python tools/run_proofread.py <file_path> [--max-chunks N]

会消耗真实API额度，--max-chunks 可限制只跑前N块。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.chunker import chunk_document  # noqa: E402
from core.parser import parse_document  # noqa: E402
from core.proofreader import LLMResponseError, proofread_chunk  # noqa: E402
from core.llm_client import LLMCallError  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    parser = argparse.ArgumentParser(description="解析→分块→校对全链路调试")
    parser.add_argument("file_path")
    parser.add_argument("--max-chunks", type=int, default=None, help="只跑前N块（省额度）")
    parser.add_argument("--save-json", default=None, help="把RawIssue原始结果存成JSON，供离线反复调规则")
    args = parser.parse_args()

    parsed = parse_document(args.file_path)
    chunked = chunk_document(parsed)
    chunks = chunked.chunks if args.max_chunks is None else chunked.chunks[: args.max_chunks]

    all_issues = []
    failed_chunks: list[str] = []

    for chunk in chunks:
        try:
            issues = proofread_chunk(chunk, parsed)
        except (LLMCallError, LLMResponseError) as exc:
            msg = f"第{chunk.chunk_index}块校对失败: {exc}"
            print(msg)
            failed_chunks.append(msg)
            continue
        all_issues.extend(issues)

    for issue in all_issues:
        located_mark = "已定位" if issue.located else "未定位"
        print(
            f"[{issue.page_location or '未知位置'}][{issue.issue_type}][{issue.category}]"
            f"[{issue.confidence}][{located_mark}] {issue.original_text} → {issue.suggestion}"
        )

    print("---")
    print(f"总条数={len(all_issues)}")
    category_counts: dict[str, int] = {}
    for issue in all_issues:
        category_counts[issue.category] = category_counts.get(issue.category, 0) + 1
    print(f"各category条数={category_counts}")
    print(f"未定位条数={sum(1 for i in all_issues if not i.located)}")
    print(f"失败块列表={failed_chunks}")

    if args.save_json:
        with open(args.save_json, "w", encoding="utf-8") as f:
            json.dump([dataclasses.asdict(issue) for issue in all_issues], f, ensure_ascii=False, indent=2)
        print(f"已保存RawIssue原始结果到: {args.save_json}")


if __name__ == "__main__":
    main()
