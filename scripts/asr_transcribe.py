#!/usr/bin/env python3
"""Command line wrapper around whisper.cpp ASR orchestrator."""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path
from typing import Iterable, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr import TranscriptionResult, WhisperCppConfig, WhisperCppTranscriber


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe WAV/PCM files using whisper.cpp",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Paths to WAV files to transcribe",
    )
    parser.add_argument(
        "--binary",
        required=True,
        type=Path,
        help="Path to whisper.cpp executable (e.g., ./whisper.cpp/bin/main)",
    )
    parser.add_argument(
        "--model",
        required=True,
        type=Path,
        help="Path to ggml-small model (e.g., models/ggml-small.bin)",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Language hint passed to whisper.cpp",
    )
    parser.add_argument(
        "--translate",
        action="store_true",
        help="Invoke whisper.cpp in translate mode",
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        dest="extra_args",
        help="Additional whisper.cpp CLI argument (repeatable)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("asr_results.txt"),
        help="Where to write transcription summaries",
    )
    return parser


def write_results(results: List[TranscriptionResult], output_path: Path) -> None:
    lines: List[str] = []
    for res in results:
        lines.append(f"# {res.audio_path}")
        lines.append(res.text.strip() or "<empty>")
        lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    cfg = WhisperCppConfig(
        binary_path=args.binary,
        model_path=args.model,
        language=args.language,
        translate=args.translate,
        extra_args=args.extra_args,
    )
    transcriber = WhisperCppTranscriber(cfg)

    results: List[TranscriptionResult] = []
    for path in args.inputs:
        res = transcriber.transcribe_file(path)
        results.append(res)
        print(f"{path}: {res.text}")

    write_results(results, args.output)
    print(f"Saved transcriptions to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
