"""Serial bridge for continuous ESP32-S3 audio capture and playback."""

from __future__ import annotations

import math
import time
from array import array
from typing import Optional, Tuple

import serial


DEFAULT_BAUD = 921_600
SERIAL_READ_TIMEOUT = 0.2  # seconds per underlying read
BYTES_PER_SAMPLE = 2
STREAM_CHUNK_BYTES = 1024
DEFAULT_HIGHPASS_CUTOFF_HZ = 250.0

AUDIO_MAGIC = 0x30445541  # 'AUD0' little-endian
AUDIO_VERSION = 1
FRAME_TYPE_AUDIO = 1
FRAME_HEADER_SIZE = 12


class HighPassFilter:
    """Simple first-order high-pass filter with int16 clamping."""

    def __init__(self, alpha: float) -> None:
        self.alpha = alpha
        self.prev_input: float = 0.0
        self.prev_output: float = 0.0

    def reset(self) -> None:
        self.prev_input = 0.0
        self.prev_output = 0.0

    def process_sample(self, sample: int) -> int:
        output = self.alpha * (self.prev_output + sample - self.prev_input)
        self.prev_input = float(sample)
        self.prev_output = output
        clamped = max(min(int(round(output)), 32767), -32768)
        return clamped


def _compute_high_pass_alpha(sample_rate: int, cutoff_hz: float = DEFAULT_HIGHPASS_CUTOFF_HZ) -> float:
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    rc = 1.0 / (2.0 * math.pi * cutoff_hz)
    dt = 1.0 / float(sample_rate)
    return rc / (rc + dt)


class SerialTimeoutError(RuntimeError):
    """Raised when the ESP does not respond within the expected time."""


class MalformedAudioHeader(RuntimeError):
    """Raised when an audio frame header from the ESP cannot be parsed."""

    def __init__(self, header: bytes) -> None:
        super().__init__(f"Malformed audio header: {header.hex()}")
        self.header = header


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
        self._serial.reset_output_buffer()
        self._rx_buffer = bytearray()
        self._capture_paused = False
        self._presence_active: Optional[bool] = None
        saw_ready = self._await_ready_banner()
        if not saw_ready and self._verbose:
            self._log("<=", "READY banner not observed; continuing")
        # Force a clean baseline regardless of initial device state
        self._send_command("PAUSE")
        self._capture_paused = True
        self.flush_input()
        self._sync_presence_state()
        self.resume_capture()
        self._high_pass_filter: Optional[HighPassFilter] = None

    # ------------------------------------------------------------------
    # Lifecycle helpers

    def close(self) -> None:
        self._serial.close()

    def _await_ready_banner(self) -> bool:
        deadline = time.monotonic() + 5.0
        while True:
            if time.monotonic() > deadline:
                return False
            line = self._serial.readline()
            if not line:
                continue
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            self._log("<=", text)
            self._handle_text_line(text)
            if text == "READY":
                return True

    def _sync_presence_state(self) -> None:
        """Request the latest PRESENCE state and wait briefly for a reply."""

        self._send_command("PRESENCE?")
        self._wait_for_presence_state(timeout=1.5)

    def _wait_for_presence_state(self, timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._presence_active is None and time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            frame = self._read_next_frame(remaining)
            if frame is not None and frame[0] == FRAME_TYPE_AUDIO:
                # Ignore any audio chunks encountered while we're paused.
                continue

    # ------------------------------------------------------------------
    # Logging helpers

    def _log(self, direction: str, message: str) -> None:
        if self._verbose:
            print(f"{direction} {message}")

    # ------------------------------------------------------------------
    # Serial primitives

    def flush_input(self) -> None:
        self._serial.reset_input_buffer()
        self._rx_buffer.clear()

    def flush_output(self) -> None:
        self._serial.reset_output_buffer()

    @property
    def presence_active(self) -> Optional[bool]:
        return self._presence_active

    def poll_presence(self, timeout: float = 0.05) -> None:
        """Drain text frames to pick up PRESENCE telemetry while capture is paused."""

        frame = self._read_next_frame(max(0.0, timeout))
        if frame is not None and frame[0] == FRAME_TYPE_AUDIO:
            # Drop any unexpected audio payload read during polling.
            return

    def read_audio_chunk(self, *, timeout: float = 1.0) -> Optional[bytes]:
        """Read the next audio frame payload as raw PCM bytes."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            frame = self._read_next_frame(remaining)
            if frame is None:
                continue
            frame_type, payload = frame
            if frame_type == FRAME_TYPE_AUDIO:
                return payload
            # Ignore non-audio frames (e.g., logs) after logging when verbose
            if frame_type is not None and self._verbose:
                self._log("<=", f"Skipped frame type {frame_type}")
        return None

    def write_line(self, line: str) -> None:
        payload = f"{line}\n".encode("ascii")
        self._log("=>", line)
        self._serial.write(payload)
        self._serial.flush()

    def _send_command(self, line: str) -> None:
        payload = f"{line}\n".encode("ascii")
        self._log("=>", line)
        self._serial.write(payload)
        self._serial.flush()

    def pause_capture(self) -> None:
        if self._capture_paused:
            return
        self._send_command("PAUSE")
        self._capture_paused = True

    def resume_capture(self) -> None:
        if not self._capture_paused:
            return
        self._send_command("RESUME")
        self._capture_paused = False

    # ------------------------------------------------------------------
    # Playback helpers

    def play_pcm(self, pcm: bytes, *, sample_rate: int) -> None:
        """Stream mono 16-bit PCM to the ESP speakers."""

        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        sample_count = len(pcm) // BYTES_PER_SAMPLE
        header = f"START {sample_rate} 1 16 {sample_count}"
        self.write_line(header)
        high_pass = self._prepare_high_pass_filter(sample_rate)
        self._stream_bytes(
            pcm,
            sample_rate * BYTES_PER_SAMPLE,
            high_pass_filter=high_pass,
        )
        self.write_line("END")

    def _prepare_high_pass_filter(self, sample_rate: int) -> HighPassFilter:
        alpha = _compute_high_pass_alpha(sample_rate)
        if self._high_pass_filter is None:
            self._high_pass_filter = HighPassFilter(alpha)
        else:
            self._high_pass_filter.alpha = alpha
            self._high_pass_filter.reset()
        return self._high_pass_filter

    def _stream_bytes(
        self,
        payload: bytes,
        bytes_per_second: int,
        *,
        high_pass_filter: Optional[HighPassFilter] = None,
    ) -> None:
        if bytes_per_second <= 0:
            raise ValueError("bytes_per_second must be positive")
        chunk_size = STREAM_CHUNK_BYTES
        next_deadline = time.perf_counter()
        for start in range(0, len(payload), chunk_size):
            end = start + chunk_size
            chunk = payload[start:end]
            if high_pass_filter is not None:
                samples = array("h")
                samples.frombytes(chunk)
                for i, sample in enumerate(samples):
                    samples[i] = high_pass_filter.process_sample(sample)
                chunk = samples.tobytes()
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

    # ------------------------------------------------------------------
    # Frame parsing helpers

    def _read_next_frame(self, timeout: float) -> Optional[Tuple[int, bytes]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self._try_extract_frame()
            if frame is not None:
                return frame
            self._fill_rx_buffer(max(0.0, deadline - time.monotonic()))
        return None

    def _fill_rx_buffer(self, timeout: float) -> None:
        read_size = self._serial.in_waiting or 1
        chunk = self._serial.read(read_size)
        if chunk:
            self._rx_buffer.extend(chunk)
        else:
            if timeout > 0:
                time.sleep(min(timeout, 0.001))

    def _try_extract_frame(self) -> Optional[Tuple[int, bytes]]:
        if not self._rx_buffer:
            return None

        newline_index = self._rx_buffer.find(b"\n")
        if newline_index != -1 and (len(self._rx_buffer) < FRAME_HEADER_SIZE or int.from_bytes(self._rx_buffer[:4], "little") != AUDIO_MAGIC):
            line = bytes(self._rx_buffer[:newline_index + 1])
            del self._rx_buffer[:newline_index + 1]
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._log("<=", text)
                self._handle_text_line(text)
            return None

        if len(self._rx_buffer) < FRAME_HEADER_SIZE:
            return None

        if int.from_bytes(self._rx_buffer[:4], "little") != AUDIO_MAGIC:
            # drop one byte and retry to resynchronise
            self._rx_buffer.pop(0)
            return None

        header = bytes(self._rx_buffer[:FRAME_HEADER_SIZE])
        version = header[4]
        frame_type = header[5]
        payload_len = int.from_bytes(header[8:12], "little")

        if version != AUDIO_VERSION or payload_len < 0 or payload_len > 4_000_000:
            del self._rx_buffer[:4]
            raise MalformedAudioHeader(header)

        total_len = FRAME_HEADER_SIZE + payload_len
        if len(self._rx_buffer) < total_len:
            return None

        payload = bytes(self._rx_buffer[FRAME_HEADER_SIZE:total_len])
        del self._rx_buffer[:total_len]
        return frame_type, payload

    def _handle_text_line(self, text: str) -> None:
        if text == "PRESENCE ON":
            self._presence_active = True
        elif text == "PRESENCE OFF":
            self._presence_active = False
