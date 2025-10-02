#!/usr/bin/env python3
"""Desktop helper to exercise the minimal ESP32-S3 audio firmware."""

from __future__ import annotations

import argparse
import contextlib
import math
import sys
import time
from array import array
from pathlib import Path
import wave

import serial

DEFAULT_BAUD = 921_600
SERIAL_READ_TIMEOUT = 0.2  # seconds per underlying read
MIC_SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2
STREAM_CHUNK_BYTES = 1024


class SerialTimeoutError(RuntimeError):
    pass


class EspAudioTester:
    """Wraps the USB serial protocol exposed by the minimal firmware."""

    def __init__(self, port: str, baudrate: int, verbose: bool = True) -> None:
        self._serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=SERIAL_READ_TIMEOUT,
            write_timeout=2.0,
        )
        self._verbose = verbose
        self._serial.reset_input_buffer()

    def close(self) -> None:
        self._serial.close()

    def read_line(self, timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = self._serial.readline()
            if raw:
                text = raw.decode("utf-8", errors="replace").strip()
                if self._verbose:
                    print(f"<= {text}")
                return text
        raise SerialTimeoutError("Timed out waiting for serial line")

    def _clear_input(self) -> None:
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

    def read_exact(self, byte_count: int, timeout: float) -> bytes:
        data = bytearray()
        deadline = time.monotonic() + timeout
        while len(data) < byte_count:
            chunk = self._serial.read(byte_count - len(data))
            if chunk:
                data.extend(chunk)
                deadline = time.monotonic() + timeout
            elif time.monotonic() > deadline:
                raise SerialTimeoutError(
                    f"Timed out reading binary payload ({len(data)}/{byte_count} bytes)"
                )
        return bytes(data)

    def record(self, seconds: int, output_path: Path) -> None:
        if seconds <= 0:
            raise ValueError("Recording length must be positive")

        target_bytes = seconds * MIC_SAMPLE_RATE * BYTES_PER_SAMPLE
        captured = bytearray()

        if self._verbose:
            print(f"   capturing {seconds}s ({target_bytes} bytes) from continuous stream")

        # Drop any buffered audio before we start counting seconds.
        self._clear_input()

        deadline = time.monotonic() + max(5.0, seconds * 1.5)
        while len(captured) < target_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SerialTimeoutError("Timed out waiting for AUDIO chunk header")

            header = self._wait_for_audio_header(timeout=remaining)
            try:
                chunk_bytes = int(header.split()[1])
            except (IndexError, ValueError) as exc:
                raise RuntimeError(f"Malformed AUDIO header: {header}") from exc

            chunk = self.read_exact(chunk_bytes, timeout=2.0)
            captured.extend(chunk)
            deadline = time.monotonic() + 2.0
            if self._verbose:
                print(f"   received {len(captured)}/{target_bytes} bytes")

        pcm = bytes(captured[:target_bytes])
        with contextlib.closing(wave.open(str(output_path), "wb")) as wav:
            wav.setnchannels(1)
            wav.setsampwidth(BYTES_PER_SAMPLE)
            wav.setframerate(MIC_SAMPLE_RATE)
            wav.writeframes(pcm)
        if self._verbose:
            print(f"   wrote WAV to {output_path} ({len(pcm)} bytes of PCM)")

    def play(self, wav_path: Path, target_rate: int) -> None:
        pcm, src_rate, channels = self._load_wav(wav_path)
        mono_pcm = self._to_mono16(pcm, channels)
        processed_pcm, playback_rate = self._resample_if_needed(mono_pcm, src_rate, target_rate)
        sample_count = len(processed_pcm) // BYTES_PER_SAMPLE

        self._clear_input()

        header = f"START {playback_rate} 1 16 {sample_count}\n".encode("ascii")
        if self._verbose:
            print(f"=> {header.decode().strip()}")
        self._serial.write(header)
        self._serial.flush()

        bytes_per_second = playback_rate * BYTES_PER_SAMPLE
        self._stream_bytes(processed_pcm, bytes_per_second)

        if self._verbose:
            print("=> END")
        self._serial.write(b"END\n")
        self._serial.flush()

    def _stream_bytes(self, payload: bytes, bytes_per_second: int) -> None:
        if bytes_per_second <= 0:
            raise ValueError("bytes_per_second must be positive")
        chunk_size = STREAM_CHUNK_BYTES
        next_deadline = time.perf_counter()
        for start in range(0, len(payload), chunk_size):
            end = start + chunk_size
            chunk = payload[start:end]
            written = self._serial.write(chunk)
            if written != len(chunk):  # pragma: no cover - serial guard
                raise SerialTimeoutError(
                    f"Short write while streaming WAV ({written}/{len(chunk)} bytes)"
                )
            self._serial.flush()
            if self._verbose:
                sent = min(end, len(payload))
                percent = min(100.0, (sent / len(payload)) * 100.0)
                print(f"   streamed {sent}/{len(payload)} bytes ({percent:.1f}%)")
            next_deadline += len(chunk) / bytes_per_second
            sleep_time = next_deadline - time.perf_counter()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                next_deadline = time.perf_counter()
        self._serial.flush()

    @staticmethod
    def _load_wav(wav_path: Path) -> tuple[bytes, int, int]:
        if wav_path.stat().st_size < 44:
            raise ValueError(f"{wav_path} is too small to be a WAV file")
        with contextlib.closing(wave.open(str(wav_path), "rb")) as wav:
            channels = wav.getnchannels()
            rate = wav.getframerate()
            sample_width = wav.getsampwidth()
            if channels < 1 or sample_width != 2:
                raise ValueError("WAV must be 16-bit PCM with at least one channel")
            pcm = wav.readframes(wav.getnframes())
        return pcm, rate, channels

    @staticmethod
    def _to_mono16(pcm: bytes, channels: int) -> bytes:
        if channels == 1:
            return pcm
        if channels <= 0:
            raise ValueError("Channel count must be positive")
        samples = array('h', pcm)
        total_frames = len(samples) // channels
        mono = array('h', [0] * total_frames)
        idx = 0
        for frame in range(total_frames):
            acc = 0
            for _ in range(channels):
                acc += samples[idx]
                idx += 1
            mono[frame] = int(round(acc / channels))
        return mono.tobytes()

    @staticmethod
    def _resample_if_needed(pcm: bytes, src_rate: int, target_rate: int) -> tuple[bytes, int]:
        if target_rate <= 0:
            target_rate = src_rate
        if target_rate == src_rate:
            return pcm, src_rate
        if target_rate > src_rate:
            raise ValueError("Upsampling is not supported")

        src_samples = array('h', pcm)
        ratio = src_rate / target_rate
        target_length = max(1, int(len(src_samples) / ratio))
        resampled = array('h', [0] * target_length)
        for i in range(target_length):
            src_index = i * ratio
            left = int(math.floor(src_index))
            right = min(left + 1, len(src_samples) - 1)
            frac = src_index - left
            if right == left:
                value = src_samples[left]
            else:
                value = src_samples[left] + (src_samples[right] - src_samples[left]) * frac
            resampled[i] = int(round(value))
        return resampled.tobytes(), target_rate

    def _wait_for_audio_header(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SerialTimeoutError("Timed out waiting for AUDIO header")
            line = self.read_line(timeout=remaining)
            if line.startswith("AUDIO "):
                return line
            # Ignore other log/state messages



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture timed audio from the continuous ESP32-S3 microphone stream and send START/END playback jobs."
        ),
    )
    parser.add_argument("--port", required=True, help="Serial port for the ESP32-S3 (e.g. /dev/ttyACM0)")
    parser.add_argument(
        "--baud",
        type=int,
        default=DEFAULT_BAUD,
        help=f"Serial baud rate (default: {DEFAULT_BAUD})",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress protocol logging; only raise on errors",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    record_cmd = sub.add_parser("record", help="Capture audio into a WAV file")
    record_cmd.add_argument("--seconds", type=int, default=5, help="Recording length (1-15 seconds)")
    record_cmd.add_argument(
        "--output",
        type=Path,
        default=Path("esp_mic_test.wav"),
        help="Where to store the captured WAV",
    )

    play_cmd = sub.add_parser("play", help="Send a WAV file for playback")
    play_cmd.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Path to a 16-bit PCM WAV file to stream",
    )
    play_cmd.add_argument(
        "--target-rate",
        type=int,
        default=16_000,
        help="Playback sample rate (<= source rate); default 16000",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.command == "record" and not (1 <= args.seconds <= 15):
        print("Record length must be between 1 and 15 seconds", file=sys.stderr)
        return 2

    try:
        tester = EspAudioTester(args.port, args.baud, verbose=not args.quiet)
    except serial.SerialException as exc:
        print(f"Failed to open serial port: {exc}", file=sys.stderr)
        return 1

    with contextlib.ExitStack() as stack:
        stack.callback(tester.close)
        try:
            if args.command == "record":
                tester.record(args.seconds, args.output)
            elif args.command == "play":
                tester.play(args.input, args.target_rate)
            else:  # pragma: no cover - argparse enforces choices
                raise ValueError(f"Unknown command {args.command}")
        except (SerialTimeoutError, RuntimeError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
