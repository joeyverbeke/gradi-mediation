#!/usr/bin/env python3
"""Quick latency check for the Vosk ASR path."""

from __future__ import annotations

import argparse
import json
import time
import wave
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe a WAV file with Vosk and report timing stats.",
    )
    parser.add_argument("audio", type=Path, help="Path to a 16 kHz mono WAV file")
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path("third_party/vosk/models/vosk-model-small-en-us-0.15"),
        help="Directory containing the Vosk model (default: third_party/vosk/models/vosk-model-small-en-us-0.15)",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=16_000,
        help="Sample rate passed to the recogniser (default: 16000 Hz)",
    )
    parser.add_argument(
        "--enable-words",
        action="store_true",
        help="Enable word-level output from Vosk",
    )
    return parser


def resolve_model_dir(candidate: Path) -> Path:
    path = candidate.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Model directory not found: {path}")
    if path.is_file():
        raise ValueError(f"Model path must be a directory: {path}")

    def has_required(root: Path) -> bool:
        return (root / "am").exists() and (root / "conf").exists()

    if has_required(path):
        return path

    subdirs = [p for p in path.iterdir() if p.is_dir()]
    if len(subdirs) == 1 and has_required(subdirs[0]):
        return subdirs[0]
    raise FileNotFoundError(
        f"Expected am/ and conf/ within Vosk model directory: {path}"
    )


def load_pcm(path: Path) -> tuple[bytes, int]:
    if not path.exists():
        raise FileNotFoundError(path)
    with wave.open(str(path), "rb") as wav_file:
        nchannels = wav_file.getnchannels()
        sampwidth = wav_file.getsampwidth()
        framerate = wav_file.getframerate()
        if sampwidth != 2:
            raise ValueError("WAV must be 16-bit PCM for Vosk")
        if nchannels != 1:
            raise ValueError("WAV must be mono for Vosk")
        pcm = wav_file.readframes(wav_file.getnframes())
    return pcm, framerate


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        from vosk import KaldiRecognizer, Model  # type: ignore
    except ImportError as exc:  # pragma: no cover - dependency missing
        parser.error("vosk is not installed. Install it via `uv pip install vosk`.")
        raise exc

    pcm, wav_rate = load_pcm(args.audio)
    if wav_rate != args.sample_rate:
        parser.error(
            f"Sample rate mismatch: WAV is {wav_rate} Hz but --sample-rate={args.sample_rate}."
        )
    try:
        model_dir = resolve_model_dir(args.model_dir)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
        raise

    load_start = time.perf_counter()
    model = Model(str(model_dir))
    load_elapsed = time.perf_counter() - load_start

    recognizer = KaldiRecognizer(model, args.sample_rate)
    if args.enable_words and hasattr(recognizer, "SetWords"):
        recognizer.SetWords(True)

    asr_start = time.perf_counter()
    recognizer.AcceptWaveform(pcm)
    result_json = recognizer.FinalResult()
    asr_elapsed = time.perf_counter() - asr_start

    data = json.loads(result_json or "{}")
    text = data.get("text", "").strip()

    print("=== Vosk Latency Report ===")
    print(f"Model path       : {model_dir}")
    print(f"Audio path       : {args.audio}")
    print(f"Sample rate      : {args.sample_rate} Hz")
    print(f"Model load       : {load_elapsed * 1000:.1f} ms")
    print(f"Transcription    : {asr_elapsed * 1000:.1f} ms")
    print("=== Transcript ===")
    print(text or "<empty>")
    if args.enable_words:
        words = data.get("result")
        if isinstance(words, list):
            print("=== Word Timings ===")
            for entry in words:
                word = entry.get("word")
                start = entry.get("start")
                end = entry.get("end")
                if word is not None:
                    print(f"[{start:.2f}s -> {end:.2f}s] {word}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
