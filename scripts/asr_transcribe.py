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

from asr import (
    FasterWhisperConfig,
    FasterWhisperTranscriber,
    TranscriptionResult,
    VoskConfig,
    VoskTranscriber,
    WhisperCppConfig,
    WhisperCppTranscriber,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe WAV/PCM files using whisper.cpp, Faster-Whisper, or Vosk",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Paths to WAV files to transcribe",
    )
    parser.add_argument(
        "--engine",
        choices=("whisper_cpp", "faster_whisper", "vosk"),
        default="whisper_cpp",
        help="ASR backend to use (default: whisper_cpp)",
    )
    parser.add_argument(
        "--binary",
        type=Path,
        help="Path to whisper.cpp executable (e.g., ./whisper.cpp/build/bin/whisper-cli)",
    )
    parser.add_argument(
        "--model",
        type=Path,
        help="Path to ggml model for whisper.cpp (e.g., models/ggml-small.bin)",
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
        "--fw-model-dir",
        type=Path,
        default=None,
        help="Directory containing Faster-Whisper model files",
    )
    parser.add_argument(
        "--fw-device",
        default="cuda",
        help="Faster-Whisper compute device (default: cuda)",
    )
    parser.add_argument(
        "--fw-compute-type",
        default="float16",
        help="Faster-Whisper compute type (default: float16)",
    )
    parser.add_argument(
        "--fw-language",
        default=None,
        help="Optional Faster-Whisper language hint",
    )
    parser.add_argument(
        "--fw-beam-size",
        type=int,
        default=1,
        help="Faster-Whisper beam size (default: 1)",
    )
    parser.add_argument(
        "--fw-temperature",
        type=float,
        default=0.0,
        help="Faster-Whisper temperature (default: 0.0)",
    )
    parser.add_argument(
        "--vosk-model-dir",
        type=Path,
        default=None,
        help="Directory containing the Vosk model (expects subdirs like conf/, am/).",
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

    if args.engine == "whisper_cpp":
        if not args.binary or not args.model:
            parser.error("--binary and --model are required when using whisper_cpp")
        cfg = WhisperCppConfig(
            binary_path=args.binary,
            model_path=args.model,
            language=args.language,
            translate=args.translate,
            extra_args=args.extra_args,
        )
        transcriber = WhisperCppTranscriber(cfg)
    elif args.engine == "faster_whisper":
        model_dir = args.fw_model_dir or Path("third_party/faster-whisper/models")
        fw_cfg = FasterWhisperConfig(
            model_dir=model_dir,
            device=args.fw_device,
            compute_type=args.fw_compute_type,
            language=args.fw_language,
            beam_size=args.fw_beam_size,
            temperature=args.fw_temperature,
        )
        transcriber = FasterWhisperTranscriber(fw_cfg)
    else:
        model_path = args.vosk_model_dir or Path("third_party/vosk/models/vosk-model-small-en-us-0.15")
        if not model_path.exists():
            parser.error(f"Vosk model directory not found: {model_path}")
        vosk_cfg = VoskConfig(model_path=model_path)
        transcriber = VoskTranscriber(vosk_cfg)

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
