#!/usr/bin/env python3
"""CLI to stream Kokoro-FastAPI speech synthesis into a local file."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tts import KokoroConfig, KokoroStreamer, SynthesisChunk


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stream text-to-speech audio from Kokoro-FastAPI",
    )
    parser.add_argument("--text", help="Text to synthesise (overrides --text-file or stdin)")
    parser.add_argument(
        "--text-file",
        type=Path,
        help="File containing the synthesis prompt",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tts_stream.wav"),
        help="Destination audio path",
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8880/v1",
        help="Base URL for Kokoro-FastAPI (default: http://127.0.0.1:8880/v1)",
    )
    parser.add_argument(
        "--endpoint",
        default="/audio/speech",
        help="Endpoint relative to the base URL (default: /audio/speech)",
    )
    parser.add_argument(
        "--model",
        default="kokoro",
        help="Model name to request (default: kokoro)",
    )
    parser.add_argument(
        "--voice",
        help="Voice identifier, e.g. af_bella or weighted combos",
    )
    parser.add_argument(
        "--response-format",
        default="wav",
        help="Audio format to request (mp3, wav, opus, flac, m4a, pcm)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        help="Playback speed factor (e.g., 1.0)",
    )
    parser.add_argument(
        "--chunk-bytes",
        type=int,
        default=32_768,
        help="Chunk size to request from the stream (bytes)",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=5.0,
        help="Connection timeout in seconds",
    )
    parser.add_argument(
        "--read-timeout",
        type=float,
        default=60.0,
        help="Read timeout in seconds",
    )
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Additional JSON payload fields forwarded to Kokoro (repeatable)",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=10,
        help="Print a progress update every N chunks (0 disables)",
    )
    return parser


def load_text(args: argparse.Namespace) -> str:
    if args.text:
        return args.text
    if args.text_file:
        if not args.text_file.exists():
            raise FileNotFoundError(args.text_file)
        return args.text_file.read_text(encoding="utf-8").strip()
    data = sys.stdin.read().strip()
    if data:
        return data
    raise SystemExit("No text provided. Use --text, --text-file, or pipe content via stdin.")


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
        if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
            return int(value)
        return float(value)
    except ValueError:
        return value


def stream_to_file(config: KokoroConfig, text: str, output_path: Path, progress_every: int) -> None:
    with KokoroStreamer(config) as streamer:
        stream = streamer.stream_synthesis(text)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        chunk_count = 0
        total_bytes = 0
        first_chunk_latency = None
        final_chunk: SynthesisChunk | None = None
        start_time = time.monotonic()

        with output_path.open("wb") as handle:
            for chunk in stream:
                if chunk.is_last:
                    final_chunk = chunk
                    break

                handle.write(chunk.data)
                chunk_count += 1
                total_bytes = chunk.total_bytes

                if chunk.first_chunk_latency_s is not None and first_chunk_latency is None:
                    first_chunk_latency = chunk.first_chunk_latency_s
                    print(f"<= first chunk arrived in {first_chunk_latency:.3f}s", flush=True)

                if progress_every > 0 and (chunk_count % progress_every) == 0:
                    print(
                        f"   wrote {total_bytes} bytes across {chunk_count} chunks",
                        flush=True,
                    )

        if final_chunk is None:
            raise RuntimeError("Kokoro stream ended without completion signal")

        total_bytes = final_chunk.total_bytes or total_bytes
        if first_chunk_latency is None and final_chunk.first_chunk_latency_s is not None:
            first_chunk_latency = final_chunk.first_chunk_latency_s

        elapsed = (
            final_chunk.elapsed_s
            if final_chunk.elapsed_s is not None
            else time.monotonic() - start_time
        )

        print(f"<= stream complete in {elapsed:.3f}s ({total_bytes} bytes, {chunk_count} chunks)", flush=True)
        if first_chunk_latency is not None:
            print(f"<= first audio chunk latency: {first_chunk_latency:.3f}s", flush=True)
        if final_chunk.content_type:
            print(f"<= content-type: {final_chunk.content_type}", flush=True)
        print(f"Saved audio to {output_path}")


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    text = load_text(args)
    extras = parse_extra(args.extra)

    try:
        config = KokoroConfig(
            base_url=args.base_url,
            endpoint=args.endpoint,
            model=args.model,
            voice=args.voice,
            response_format=args.response_format,
            speed=args.speed,
            stream_chunk_bytes=args.chunk_bytes,
            connect_timeout=args.connect_timeout,
            read_timeout=args.read_timeout,
            extra_payload=dict(extras),
        )
    except ValueError as exc:
        parser.error(str(exc))

    print(
        f"=> POST {config.build_url()} (model={config.model}, voice={config.voice or 'default'}, format={config.response_format})",
        flush=True,
    )
    stream_to_file(config, text, args.output, args.progress_every)
    return 0


if __name__ == "__main__":
    sys.exit(main())
