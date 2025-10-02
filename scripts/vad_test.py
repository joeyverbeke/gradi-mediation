#!/usr/bin/env python3
"""CLI to exercise the desktop VAD module against WAV inputs."""

from __future__ import annotations

import argparse
import contextlib
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop_vad import VADConfig, WebRTCVADProcessor


@dataclass
class WavData:
    frames: bytes
    sample_rate: int
    channels: int


def load_wav(path: Path) -> WavData:
    if not path.exists():
        raise FileNotFoundError(path)

    with contextlib.closing(wave.open(str(path), "rb")) as wav:
        channels = wav.getnchannels()
        sample_rate = wav.getframerate()
        sample_width = wav.getsampwidth()
        if sample_width != 2:
            raise ValueError("Only 16-bit PCM WAV files are supported")
        frames = wav.readframes(wav.getnframes())
    return WavData(frames=frames, sample_rate=sample_rate, channels=channels)


def require_mono(data: WavData) -> WavData:
    if data.channels != 1:
        raise ValueError("Input WAV must be mono. Downmix before running VAD.")
    return data


def detect_segments(data: WavData, cfg: VADConfig) -> List[Tuple[float, float]]:
    processor = WebRTCVADProcessor(cfg)
    return processor.process(data.frames)


def print_segments(segments: Iterable[Tuple[float, float]]) -> None:
    any_segment = False
    for start, end in segments:
        any_segment = True
        print(f"speech: {start:.3f}s -> {end:.3f}s")
    if not any_segment:
        print("no speech detected")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect speech regions using WebRTC VAD",
    )
    parser.add_argument("input", type=Path, help="Path to 16-bit mono WAV file")
    parser.add_argument(
        "--frame-ms",
        type=int,
        default=30,
        choices=(10, 20, 30),
        help="Frame size in milliseconds (default: 30)",
    )
    parser.add_argument(
        "--aggressiveness",
        type=int,
        default=2,
        choices=range(4),
        help="VAD aggressiveness (0-3, default: 2)",
    )
    parser.add_argument(
        "--start-trigger",
        type=int,
        default=3,
        help="Consecutive speech frames required to start a segment",
    )
    parser.add_argument(
        "--stop-trigger",
        type=int,
        default=5,
        help="Consecutive non-speech frames required to end a segment",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    wav_data = require_mono(load_wav(args.input))

    cfg = VADConfig(
        sample_rate=wav_data.sample_rate,
        frame_duration_ms=args.frame_ms,
        aggressiveness=args.aggressiveness,
        start_trigger_frames=args.start_trigger,
        stop_trigger_frames=args.stop_trigger,
    )

    segments = detect_segments(wav_data, cfg)
    print_segments(segments)
    return 0


if __name__ == "__main__":
    sys.exit(main())
