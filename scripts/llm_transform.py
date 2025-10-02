#!/usr/bin/env python3
"""CLI to run the vLLM-based make-it-perfect transform over transcripts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable, List, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from llm import TransformResult, VLLMConfig, VLLMTransformer


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clean ASR transcripts using the local vLLM server",
    )
    parser.add_argument(
        "sources",
        nargs="*",
        type=Path,
        help="Optional text files with transcripts (defaults skip lines starting with #)",
    )
    parser.add_argument(
        "--text",
        action="append",
        default=[],
        dest="texts",
        help="Inline transcript text (repeatable)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("llm_pairs.jsonl"),
        help="Where to write JSONL input/output pairs",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000/v1",
        help="vLLM server base URL (default: http://127.0.0.1:8000/v1)",
    )
    parser.add_argument(
        "--model",
        default="hugging-quants/Meta-Llama-3.1-8B-Instruct-GPTQ-INT4",
        help="Model name configured in vLLM",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=128,
        help="Maximum tokens to generate per completion",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.2,
        help="Sampling temperature passed to vLLM",
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=0.9,
        help="Top-p nucleus sampling parameter",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout when calling vLLM",
    )
    return parser


def load_transcripts(paths: Sequence[Path]) -> List[str]:
    transcripts: List[str] = []
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)
        text = path.read_text(encoding="utf-8")
        transcripts.extend(parse_transcript_blocks(text))
    return transcripts


def parse_transcript_blocks(raw_text: str) -> List[str]:
    blocks: List[str] = []
    current: List[str] = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                blocks.append(" ".join(current).strip())
                current.clear()
            continue
        if stripped.startswith("#"):
            continue
        current.append(stripped)
    if current:
        blocks.append(" ".join(current).strip())
    return [b for b in blocks if b]


def merge_inputs(file_texts: Sequence[str], inline_texts: Sequence[str]) -> List[str]:
    merged: List[str] = []
    for text in file_texts:
        if text.strip():
            merged.append(text.strip())
    for text in inline_texts:
        if text.strip():
            merged.append(text.strip())
    return merged


def run_transform(transformer: VLLMTransformer, texts: Iterable[str]) -> List[TransformResult]:
    results: List[TransformResult] = []
    for text in texts:
        results.append(transformer.transform(text))
    return results


def write_jsonl(results: Sequence[TransformResult], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fp:
        for item in results:
            payload = {"input": item.input_text, "output": item.output_text}
            fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def print_results(results: Sequence[TransformResult]) -> None:
    for item in results:
        print("IN:", item.input_text)
        print("OUT:", item.output_text)
        print("---")


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    from_files = load_transcripts(args.sources)
    transcripts = merge_inputs(from_files, args.texts)

    if not transcripts:
        parser.error("No transcripts provided. Use --text or supply input files with content.")

    cfg = VLLMConfig(
        base_url=args.base_url,
        model=args.model,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.timeout,
    )

    with VLLMTransformer(cfg) as transformer:
        results = run_transform(transformer, transcripts)

    write_jsonl(results, args.output)
    print_results(results)
    print(f"Saved {len(results)} pairs to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
