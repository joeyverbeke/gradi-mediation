#!/usr/bin/env python3
"""Run the end-to-end session controller against the ESP bridge."""

from __future__ import annotations

import argparse
import contextlib
import json
from datetime import datetime
import sys
from pathlib import Path
from typing import Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asr import WhisperCppConfig, WhisperCppTranscriber
from controller import ESPAudioBridge, SessionController, SessionControllerConfig
from desktop_vad import VADConfig
from llm import VLLMConfig, VLLMTransformer
from tts import KokoroConfig, KokoroStreamer


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Coordinate capture → ASR → LLM → TTS playback cycles",
    )
    parser.add_argument("--port", required=True, help="Serial port for the ESP32-S3 (e.g. /dev/ttyACM0)")
    parser.add_argument("--baud", type=int, default=921_600, help="Serial baudrate (default 921600)")
    parser.add_argument("--whisper-binary", type=Path, required=True, help="Path to whisper.cpp executable")
    parser.add_argument("--whisper-model", type=Path, required=True, help="Path to ggml model file")
    parser.add_argument("--llm-base-url", default="http://127.0.0.1:8000/v1", help="vLLM base URL")
    parser.add_argument(
        "--llm-model",
        default="hugging-quants/Meta-Llama-3.1-8B-Instruct-GPTQ-INT4",
        help="Model name exposed by vLLM",
    )
    parser.add_argument("--kokoro-base-url", default="http://127.0.0.1:8880/v1", help="Kokoro base URL")
    parser.add_argument("--kokoro-endpoint", default="/audio/speech", help="Kokoro endpoint path")
    parser.add_argument("--kokoro-voice", help="Voice identifier (e.g. af_bella)")
    parser.add_argument(
        "--kokoro-format",
        default="pcm",
        choices=("pcm", "wav", "mp3", "flac", "opus", "m4a"),
        help="Requested audio format from Kokoro",
    )
    parser.add_argument(
        "--kokoro-extra",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Additional JSON payload fields forwarded to Kokoro (repeatable)",
    )
    parser.add_argument("--playback-rate", type=int, default=16_000, help="Playback sample rate for ESP (default 16000)")
    parser.add_argument("--tts-expected-rate", type=int, default=24_000, help="Expected Kokoro sample rate")
    parser.add_argument("--log-dir", type=Path, default=Path("logs/sessions"), help="Directory to write JSONL session logs")
    parser.add_argument("--max-cycles", type=int, default=None, help="Stop after N cycles (default unlimited)")
    parser.add_argument("--vad-frame-ms", type=int, default=30, choices=(10, 20, 30))
    parser.add_argument("--vad-aggressiveness", type=int, default=2, choices=range(4))
    parser.add_argument("--vad-start-frames", type=int, default=3)
    parser.add_argument("--vad-stop-frames", type=int, default=5)
    parser.add_argument("--verbose-esp", action="store_true", help="Print raw ESP protocol logs")
    return parser


def parse_extra(values: Iterable[str]) -> Mapping[str, object]:
    extras = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"Invalid extra payload entry (expected key=value): {item}")
        key, raw_value = item.split("=", 1)
        extras[key.strip()] = auto_cast(raw_value.strip())
    return extras


def auto_cast(value: str) -> object:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return value


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        extras = parse_extra(args.kokoro_extra)
    except ValueError as exc:
        parser.error(str(exc))
        return 2

    log_path = None
    if args.log_dir is not None:
        args.log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        log_path = args.log_dir / f'session_{timestamp}.jsonl'

    vad_cfg = VADConfig(
        sample_rate=16_000,
        frame_duration_ms=args.vad_frame_ms,
        aggressiveness=args.vad_aggressiveness,
        start_trigger_frames=args.vad_start_frames,
        stop_trigger_frames=args.vad_stop_frames,
    )

    whisper_cfg = WhisperCppConfig(
        binary_path=args.whisper_binary,
        model_path=args.whisper_model,
        sample_rate=vad_cfg.sample_rate,
    )
    llm_cfg = VLLMConfig(base_url=args.llm_base_url, model=args.llm_model)
    kokoro_cfg = KokoroConfig(
        base_url=args.kokoro_base_url,
        endpoint=args.kokoro_endpoint,
        model="kokoro",
        voice=args.kokoro_voice,
        response_format=args.kokoro_format,
        extra_payload=dict(extras),
    )

    controller_cfg = SessionControllerConfig(
        sample_rate=vad_cfg.sample_rate,
        playback_sample_rate=args.playback_rate,
        vad_config=vad_cfg,
        tts_expected_sample_rate=args.tts_expected_rate,
        log_path=log_path,
    )

    try:
        esp = ESPAudioBridge(args.port, args.baud, verbose=args.verbose_esp)
    except Exception as exc:
        print(f"Failed to open serial port: {exc}", file=sys.stderr)
        return 1

    transcriber = WhisperCppTranscriber(whisper_cfg)
    transformer = VLLMTransformer(llm_cfg)
    streamer = KokoroStreamer(kokoro_cfg)

    controller = SessionController(
        esp=esp,
        asr=transcriber,
        llm=transformer,
        tts=streamer,
        config=controller_cfg,
    )

    with contextlib.ExitStack() as stack:
        stack.callback(controller.close)
        stack.callback(esp.close)
        stack.callback(streamer.close)
        stack.callback(transformer.close)
        try:
            controller.run(max_cycles=args.max_cycles)
        except KeyboardInterrupt:
            print("\nInterrupted", file=sys.stderr)
        except Exception as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    if log_path is not None:
        print(f"Logs written to {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
