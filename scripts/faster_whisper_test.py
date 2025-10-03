#!/usr/bin/env python3
"""Minimal latency sanity-check for Faster-Whisper on this project."""

from __future__ import annotations

import argparse
import time
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe an audio file with Faster-Whisper and report timing stats.",
    )
    parser.add_argument("audio", type=Path, help="Path to the audio file to transcribe (wav/mp3/flac/etc.)")
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path("third_party/faster-whisper/models"),
        help="Directory containing the Faster-Whisper model files (default: third_party/faster-whisper/models).",
    )
    parser.add_argument(
        "--device",
        default="cuda",
        choices=("cuda", "cpu"),
        help="Compute device to use (default: cuda).",
    )
    parser.add_argument(
        "--compute-type",
        default="float16",
        help="Faster-Whisper compute type, e.g. float16, float32, int8_float16 (default: float16).",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Optional language hint (ISO 639-1/2 code).",
    )
    parser.add_argument(
        "--beam-size",
        type=int,
        default=1,
        help="Beam size for decoding (default: 1 for greedy).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Sampling temperature (default: 0, deterministic).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover - missing dependency
        parser.error(
            "faster-whisper is not installed. Install it via `uv pip install faster-whisper`."
        )
        raise exc

    if not args.audio.exists():
        parser.error(f"Audio file not found: {args.audio}")
    if not args.model_dir.exists():
        parser.error(f"Model directory not found: {args.model_dir}")

    load_start = time.perf_counter()
    model = WhisperModel(
        str(args.model_dir),
        device=args.device,
        compute_type=args.compute_type,
    )
    load_elapsed = time.perf_counter() - load_start

    transcribe_start = time.perf_counter()
    segments, info = model.transcribe(
        str(args.audio),
        beam_size=args.beam_size,
        temperature=args.temperature,
        language=args.language,
    )
    transcribe_elapsed = time.perf_counter() - transcribe_start

    total_elapsed = load_elapsed + transcribe_elapsed
    audio_duration = getattr(info, "duration", 0.0) or 0.0
    rtf = (transcribe_elapsed / audio_duration) if audio_duration else 0.0

    print("=== Faster-Whisper Latency Report ===")
    print(f"Model path        : {args.model_dir}")
    print(f"Device / compute  : {args.device} / {args.compute_type}")
    print(f"Audio path        : {args.audio}")
    print(f"Audio duration    : {audio_duration:.2f} s")
    print(f"Model load        : {load_elapsed*1000:.1f} ms")
    print(f"Transcription     : {transcribe_elapsed*1000:.1f} ms")
    print(f"Total elapsed     : {total_elapsed*1000:.1f} ms")
    print(f"Real-time factor  : {rtf:.3f}")
    print("=== Transcript ===")
    for segment in segments:
        print(f"[{segment.start:.2f}s -> {segment.end:.2f}s] {segment.text.strip()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
