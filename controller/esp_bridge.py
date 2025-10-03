"""Serial bridge for continuous ESP32-S3 audio capture and playback."""

from __future__ import annotations

import time
from typing import Optional

import serial


DEFAULT_BAUD = 921_600
SERIAL_READ_TIMEOUT = 0.2  # seconds per underlying read
MIC_SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2
STREAM_CHUNK_BYTES = 1024


class SerialTimeoutError(RuntimeError):
    """Raised when the ESP does not respond within the expected time."""


class ESPAudioBridge:
    """High-level helper to interact with the ESP audio firmware."""

    def __init__(
        self,
        port: str,
        baudrate: int = DEFAULT_BAUD,
        *,
        read_timeout: float = SERIAL_READ_TIMEOUT,
        write_timeout: float = 2.0,
        verbose: bool = False,
    ) -> None:
        self._serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=read_timeout,
            write_timeout=write_timeout,
        )
        self._verbose = verbose
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

    # ------------------------------------------------------------------
    # Lifecycle helpers

    def close(self) -> None:
        self._serial.close()

    # ------------------------------------------------------------------
    # Logging helpers

    def _log(self, direction: str, message: str) -> None:
        if self._verbose:
            print(f"{direction} {message}")

    # ------------------------------------------------------------------
    # Serial primitives

    def flush_input(self) -> None:
        self._serial.reset_input_buffer()

    def flush_output(self) -> None:
        self._serial.reset_output_buffer()

    def read_audio_chunk(self, *, timeout: float = 1.0) -> Optional[bytes]:
        """Read the next AUDIO chunk from the ESP stream.

        Returns ``None`` if no chunk arrives within ``timeout`` seconds.
        """

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                header = self._wait_for_audio_header(timeout=remaining)
            except SerialTimeoutError:
                return None

            if header is None:
                return None

            try:
                byte_count = int(header.split()[1])
            except (IndexError, ValueError) as exc:
                raise RuntimeError(f"Malformed AUDIO header: {header}") from exc

            payload = self._read_exact(byte_count, timeout=2.0)
            return payload
        return None

    def _wait_for_audio_header(self, *, timeout: float) -> Optional[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            line = self._serial.readline()
            if not line:
                continue
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            if text.startswith("AUDIO "):
                self._log("<=", text)
                return text
            # Forward other log lines to stdout when verbose
            self._log("<=", text)
        return None

    def _read_exact(self, byte_count: int, timeout: float) -> bytes:
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

    def write_line(self, line: str) -> None:
        payload = f"{line}\n".encode("ascii")
        self._log("=>", line)
        self._serial.write(payload)
        self._serial.flush()

    # ------------------------------------------------------------------
    # Playback helpers

    def play_pcm(self, pcm: bytes, *, sample_rate: int) -> None:
        """Stream mono 16-bit PCM to the ESP speakers."""

        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        sample_count = len(pcm) // BYTES_PER_SAMPLE
        header = f"START {sample_rate} 1 16 {sample_count}"
        self.write_line(header)
        self._stream_bytes(pcm, sample_rate * BYTES_PER_SAMPLE)
        self.write_line("END")

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
            next_deadline += len(chunk) / bytes_per_second
            sleep_time = next_deadline - time.perf_counter()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                next_deadline = time.perf_counter()
        self._serial.flush()

