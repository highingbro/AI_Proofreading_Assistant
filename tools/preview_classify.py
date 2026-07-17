"""阶段5调试辅助脚本：预览 core/classifier.py 的归层结果。

两种用法：
    python tools/preview_classify.py <file_path> [--max-chunks N]
        全链路：解析→分块→校对→分层，会消耗真实API额度。

    python tools/preview_classify.py <file_path> --from-json <path>
        仍会解析文件（不耗额度，只是为规则C提供 ParsedDocument 做OCR置信度查询），
        但跳过分块+LLM校对，直接读取 tools/run_proofread.py --save-json 保存的
        RawIssue JSON 做分层，便于零成本反复调规则。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from core.chunker import chunk_document  # noqa: E402
from core.classifier import classify_issues  # noqa: E402
from core.llm_client import LLMCallError  # noqa: E402
from core.parser import parse_document  # noqa: E402
from core.proofreader import LLMResponseError, ProofreadResult, RawIssue, proofread_chunk  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _run_full_pipeline(file_path: str, max_chunks: int | None) -> tuple[ProofreadResult, object]:
    parsed = parse_document(file_path)
    chunked = chunk_document(parsed)
    chunks = chunked.chunks if max_chunks is None else chunked.chunks[:max_chunks]

    issues = []
    chunk_warnings: list[str] = []
    for chunk in chunks:
        try:
            issues.extend(proofread_chunk(chunk, parsed))
        except (LLMCallError, LLMResponseError) as exc:
            msg = f"第{chunk.chunk_index}块校对失败: {exc}"
            print(msg)
            chunk_warnings.append(msg)

    return ProofreadResult(issues=issues, chunk_warnings=chunk_warnings), parsed


def _load_from_json(file_path: str, json_path: str) -> tuple[ProofreadResult, object]:
    parsed = parse_document(file_path)
    with open(json_path, encoding="utf-8") as f:
        raw_dicts = json.load(f)
    issues = [RawIssue(**d) for d in raw_dicts]
    return ProofreadResult(issues=issues, chunk_warnings=[]), parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="校对结果分层预览")
    parser.add_argument("file_path")
    parser.add_argument("--max-chunks", type=int, default=None, help="全链路模式下只跑前N块（省额度）")
    parser.add_argument("--from-json", default=None, help="从--save-json保存的RawIssue JSON直接分层，不耗额度")
    parser.add_argument(
        "--mode", default=config.PROOFREAD_MODE_DEEP, choices=config.PROOFREAD_MODES, help="校对模式（精简/深度），默认深度"
    )
    args = parser.parse_args()

    if args.from_json:
        result, parsed = _load_from_json(args.file_path, args.from_json)
    else:
        result, parsed = _run_full_pipeline(args.file_path, args.max_chunks)

    classified = classify_issues(result, parsed, mode=args.mode)

    for layer in config.LAYERS:
        layer_issues = [i for i in classified.issues if i.layer == layer]
        if not layer_issues:
            continue
        print(f"===== {layer}（{len(layer_issues)}条）=====")
        for issue in layer_issues:
            print(
                f"[{issue.layer}][{issue.priority}][{issue.page_location or '未知位置'}][{issue.issue_type}] "
                f"{issue.original_text} → {issue.suggestion} (依据: {'; '.join(issue.layer_notes)})"
            )

    print("---")
    print(f"stats={classified.stats}")
    print(f"warnings={classified.warnings}")


if __name__ == "__main__":
    main()
