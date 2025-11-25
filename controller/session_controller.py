"""Session controller that coordinates VAD, ASR, LLM, and TTS for the ESP bridge."""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from array import array
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from asr import ASRTranscriber, TranscriptionResult
from desktop_vad import VADConfig
from llm import TransformResult, VLLMTransformer
from tts import KokoroStreamer

from .esp_bridge import ESPAudioBridge, MalformedAudioHeader
from .vad_stream import SpeechSegment, SpeechStartEvent, VADStream

BLANK_TRANSCRIPT_MARKERS = {
    "[BLANK_AUDIO]",
    "[BLANK]",
    "[SILENCE]",
    "[EMPTY]",
    "[NO_SPEECH]",
}

PARENTHETICAL_NOISE_TOKENS = {
    "music",
    "upbeat music",
    "background music",
    "applause",
    "laughter",
    "silence",
    "noise",
    "static",
}

PUNCT_ONLY_CHARSET = set(".,!?:;-'\"()[]{} ")

LOGGER = logging.getLogger("session_controller")
if not LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(handler)
LOGGER.setLevel(logging.INFO)


@dataclass(frozen=True)
class SessionControllerConfig:
    """Configuration knobs for the orchestrator."""

    sample_rate: int = 16_000
    playback_sample_rate: int = 16_000
    playback_gain_db: float = 0.0
    vad_config: VADConfig = field(default_factory=VADConfig)
    vad_preroll_frames: int = 2
    max_capture_seconds: Optional[float] = None
    min_segment_duration: float = 0.3
    min_mean_abs_amplitude: float = 200.0
    capture_resume_delay: float = 0.75
    asr_timeout: float = 15.0
    llm_timeout: float = 20.0
    tts_first_chunk_timeout: float = 5.0
    playback_timeout: float = 20.0
    tts_expected_sample_rate: int = 24_000
    log_path: Optional[Path] = None


class SessionController:
    """High-level orchestrator that binds all desktop modules."""

    def __init__(
        self,
        *,
        esp: ESPAudioBridge,
        asr: ASRTranscriber,
        llm: VLLMTransformer,
        tts: KokoroStreamer,
        config: SessionControllerConfig,
    ) -> None:
        self.esp = esp
        self.asr = asr
        self.llm = llm
        self.tts = tts
        self.config = config

        self.vad_stream = VADStream(config.vad_config, preroll_frames=config.vad_preroll_frames)

        self.state = "Idle"
        self._processing_segment = False
        self._current_session_id: Optional[str] = None
        self._capture_started_at: Optional[float] = None
        self._stop_requested = False
        self._capture_suspended_until: float = 0.0
        self._presence_state: Optional[bool] = None

        self._log_path = config.log_path
        self._log_file = None
        if self._log_path is not None:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = self._log_path.open("a", encoding="utf-8")

    # ------------------------------------------------------------------
    # Public API

    def stop(self) -> None:
        self._stop_requested = True

    def close(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None

    def run(self, *, max_cycles: Optional[int] = None) -> None:
        cycles_completed = 0
        self._transition("Idle", reason="controller.start")

        while not self._stop_requested:
            if max_cycles is not None and cycles_completed >= max_cycles:
                break

            if self._processing_segment:
                # Drain serial input but do not feed VAD while busy.
                self.esp.read_audio_chunk(timeout=0.2)
                continue

            if (
                self.config.max_capture_seconds is not None
                and self._capture_started_at is not None
                and self.state == "CaptureRequested"
            ):
                elapsed = time.monotonic() - self._capture_started_at
                if elapsed > self.config.max_capture_seconds:
                    self._transition(
                        "CaptureRequested",
                        reason="capture.timeout_6s",
                        duration=elapsed,
                    )
                    segment = self.vad_stream.force_close()
                    if segment is not None:
                        success = self._handle_segment(segment, allow_timeout_segment=True)
                        if success:
                            cycles_completed += 1
                            idle_reason = "cycle.complete"
                        else:
                            idle_reason = "cycle.discarded"
                        self._transition("Idle", reason=idle_reason, cycles=cycles_completed)
                        self._current_session_id = None
                        self._capture_started_at = None
                        if max_cycles is not None and cycles_completed >= max_cycles:
                            return
                    else:
                        self.vad_stream.reset()
                        self.esp.flush_input()
                        self._transition("Idle", reason="capture.timeout")
                        self._current_session_id = None
                        self._capture_started_at = None
                    continue

            if self._presence_blocks_capture():
                continue

            try:
                chunk = self.esp.read_audio_chunk(timeout=0.5)
            except MalformedAudioHeader as exc:
                self._transition(
                    "FatalError",
                    stage="capture",
                    reason="malformed_audio_header",
                    header=exc.header.hex(),
                    error_type=exc.__class__.__name__,
                )
                raise
            except Exception as exc:  # pragma: no cover - defensive guard
                self._transition(
                    "FatalError",
                    stage="capture",
                    reason="audio_read_failed",
                    error=str(exc),
                    error_type=exc.__class__.__name__,
                )
                raise
            if chunk is None:
                continue

            if time.monotonic() < self._capture_suspended_until:
                # Drop audio while waiting for playback tail to settle.
                continue

            for event in self.vad_stream.add_audio(chunk):
                if isinstance(event, SpeechStartEvent):
                    self._handle_capture_start(event)
                elif isinstance(event, SpeechSegment):
                    success = self._handle_segment(event)
                    if success:
                        cycles_completed += 1
                        idle_reason = "cycle.complete"
                    else:
                        idle_reason = "cycle.discarded"
                    self._transition("Idle", reason=idle_reason, cycles=cycles_completed)
                    self._current_session_id = None
                    self._capture_started_at = None
                    if max_cycles is not None and cycles_completed >= max_cycles:
                        return
                else:  # pragma: no cover - defensive
                    continue

    # ------------------------------------------------------------------
    # Event handlers

    def _handle_capture_start(self, event: SpeechStartEvent) -> None:
        if self._processing_segment:
            return
        self._current_session_id = uuid.uuid4().hex[:8]
        self._capture_started_at = time.monotonic()
        self._transition(
            "CaptureRequested",
            start_time=event.start_time,
            start_byte=event.start_byte,
        )

    def _handle_segment(self, segment: SpeechSegment, *, allow_timeout_segment: bool = False) -> bool:
        if self._current_session_id is None:
            self._current_session_id = uuid.uuid4().hex[:8]
        self._processing_segment = True

        segment_duration = segment.end_time - segment.start_time
        if segment_duration < self.config.min_segment_duration:
            self._transition(
                "ReturnToIdle",
                reason="segment.discarded",
                cause="segment.too_short",
                duration=segment_duration,
            )
            self._processing_segment = False
            self.esp.flush_input()
            return False

        if not allow_timeout_segment:
            if (
                self.config.max_capture_seconds is not None
                and segment_duration > self.config.max_capture_seconds
            ):
                self._transition(
                    "ErrorTimeout",
                    stage="capture",
                    reason="segment.too_long",
                    duration=segment_duration,
                )
                self._processing_segment = False
                return False

        mean_abs_amplitude = self._segment_mean_abs_amplitude(segment.pcm)
        if mean_abs_amplitude < self.config.min_mean_abs_amplitude:
            self._transition(
                "ReturnToIdle",
                reason="segment.discarded",
                cause="low_energy",
                mean_abs=int(mean_abs_amplitude),
                duration=segment_duration,
            )
            self._processing_segment = False
            self.esp.flush_input()
            return False

        try:
            asr_result = self._run_asr(segment)
            transcript = asr_result.text.strip()
            if self._is_blank_transcript(transcript):
                self._transition("ReturnToIdle", reason="segment.discarded", cause="blank_transcript")
                return False
            llm_result = self._run_llm(asr_result)
            if self._is_invalid_llm_output(llm_result.output_text):
                self._transition(
                    "ReturnToIdle",
                    reason="segment.discarded",
                    cause="llm_diagnostic",
                    llm_preview=self._truncate(llm_result.output_text),
                )
                return False
            playback_meta = self._run_tts_and_play(llm_result)
            self._transition(
                "ReturnToIdle",
                reason="playback.complete",
                playback=playback_meta,
            )
            self._capture_suspended_until = time.monotonic() + self.config.capture_resume_delay
            self.vad_stream.reset()
            return True
        except Exception as exc:  # pragma: no cover - error path
            self._transition(
                "ErrorTimeout",
                stage="pipeline",
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            return False
        finally:
            self._processing_segment = False
            self.esp.flush_input()

    # ------------------------------------------------------------------
    # Pipeline stages

    def _run_asr(self, segment: SpeechSegment) -> TranscriptionResult:
        start = time.monotonic()
        self._transition(
            "ASR",
            reason="segment.complete",
            duration=segment.end_time - segment.start_time,
        )
        result = self.asr.transcribe_pcm(segment.pcm, sample_rate=self.config.sample_rate)
        latency = time.monotonic() - start
        self._transition(
            "ASR",
            reason="complete",
            latency_ms=int(latency * 1000),
            text_preview=self._truncate(result.text),
        )
        if latency > self.config.asr_timeout:
            raise RuntimeError(f"ASR exceeded timeout ({latency:.2f}s)")
        return result

    def _run_llm(self, asr_result: TranscriptionResult) -> TransformResult:
        start = time.monotonic()
        self._transition(
            "LLMTransform",
            reason="start",
            transcript_preview=self._truncate(asr_result.text),
        )
        result = self.llm.transform(asr_result.text)
        latency = time.monotonic() - start
        self._transition(
            "LLMTransform",
            reason="complete",
            latency_ms=int(latency * 1000),
            output_preview=self._truncate(result.output_text),
        )
        if latency > self.config.llm_timeout:
            raise RuntimeError(f"LLM exceeded timeout ({latency:.2f}s)")
        return result

    def _run_tts_and_play(self, llm_result: TransformResult) -> dict:
        start = time.monotonic()
        self._transition(
            "TTSSynthesis",
            reason="start",
            text_preview=self._truncate(llm_result.output_text),
        )

        pcm_buffer = bytearray()
        first_chunk_latency: Optional[float] = None
        headers = {}
        content_type: Optional[str] = None

        total_bytes = 0
        elapsed: Optional[float] = None
        for chunk in self.tts.stream_synthesis(llm_result.output_text):
            if chunk.headers:
                headers.update(chunk.headers)
            if chunk.content_type:
                content_type = chunk.content_type
            if chunk.is_last:
                elapsed = chunk.elapsed_s or (time.monotonic() - start)
                total_bytes = chunk.total_bytes
                first_chunk_latency = first_chunk_latency or chunk.first_chunk_latency_s
                break
            pcm_buffer.extend(chunk.data)
            if first_chunk_latency is None and chunk.first_chunk_latency_s is not None:
                first_chunk_latency = chunk.first_chunk_latency_s
        else:  # pragma: no cover - defensive guard
            elapsed = time.monotonic() - start
            total_bytes = len(pcm_buffer)

        tts_latency = time.monotonic() - start
        self._transition(
            "TTSSynthesis",
            reason="complete",
            latency_ms=int(tts_latency * 1000),
            first_chunk_ms=int((first_chunk_latency or 0) * 1000),
        )
        if first_chunk_latency is not None and first_chunk_latency > self.config.tts_first_chunk_timeout:
            raise RuntimeError(f"TTS first chunk exceeded timeout ({first_chunk_latency:.2f}s)")

        pcm = bytes(pcm_buffer)
        if not pcm:
            raise RuntimeError('Kokoro synthesis returned no audio data')
        if total_bytes and total_bytes != len(pcm):
            # When streaming response omits trailing data we rely on buffer length
            total_bytes = len(pcm)

        sample_rate = self._infer_sample_rate(headers, content_type)
        if sample_rate is None:
            sample_rate = self.config.tts_expected_sample_rate

        pcm, sample_rate = self._resample_if_needed(pcm, sample_rate, self.config.playback_sample_rate)
        pcm = self._apply_gain(pcm, self.config.playback_gain_db)
        playback_start = time.monotonic()
        self._transition(
            "Playback",
            reason="start",
            sample_rate=sample_rate,
            bytes=len(pcm),
        )
        self.esp.pause_capture()
        try:
            self.esp.flush_input()
            self.esp.play_pcm(pcm, sample_rate=sample_rate)
        finally:
            self.esp.resume_capture()
        playback_elapsed = time.monotonic() - playback_start
        self._transition(
            "Playback",
            reason="complete",
            duration_ms=int(playback_elapsed * 1000),
        )
        if playback_elapsed > self.config.playback_timeout:
            raise RuntimeError(f"Playback exceeded timeout ({playback_elapsed:.2f}s)")

        return {
            "tts_first_chunk_ms": int((first_chunk_latency or 0) * 1000),
            "tts_elapsed_ms": int((elapsed if elapsed is not None else 0.0) * 1000),
            "playback_ms": int(playback_elapsed * 1000),
            "pcm_bytes": len(pcm),
            "sample_rate": sample_rate,
        }

    # ------------------------------------------------------------------
    # Helpers

    def _transition(self, state: str, **metadata) -> None:
        self.state = state
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "state": state,
        }
        if self._current_session_id:
            payload["session"] = self._current_session_id
        payload.update(metadata)
        line = json.dumps(payload, ensure_ascii=False)
        LOGGER.info(line)
        if self._log_file is not None:
            self._log_file.write(line + "\n")
            self._log_file.flush()

    @staticmethod
    def _truncate(text: str, *, limit: int = 120) -> str:
        text = text.strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    @staticmethod
    def _is_blank_transcript(text: str) -> bool:
        if not text:
            return True
        normalized = text.strip()
        if not normalized:
            return True
        upper = normalized.upper()
        if upper in BLANK_TRANSCRIPT_MARKERS:
            return True

        lower = normalized.lower()
        if normalized.startswith("(") and normalized.endswith(")"):
            inner = lower.strip("() ")
            if not inner:
                return True
            if any(token in inner for token in PARENTHETICAL_NOISE_TOKENS):
                return True

        if normalized.startswith("[") and normalized.endswith("]"):
            inner = lower.strip("[] ")
            if inner in {token.lower() for token in BLANK_TRANSCRIPT_MARKERS}:
                return True

        if all(ch in PUNCT_ONLY_CHARSET for ch in normalized):
            return True

        if lower in {"(upbeat music)", "(background noise)", "(silence)"}:
            return True

        return False

    def _presence_blocks_capture(self) -> bool:
        presence = self.esp.presence_active
        if presence is None:
            self.esp.poll_presence(timeout=0.05)
            return False

        if presence is False:
            if self._presence_state is not False:
                self._transition("PresenceIdle", reason="presence.off")
                self.vad_stream.reset()
                self.esp.flush_input()
                self._current_session_id = None
                self._capture_started_at = None
            self._presence_state = False
            self.esp.poll_presence(timeout=0.05)
            time.sleep(0.05)
            return True

        if self._presence_state is False:
            self._transition("PresenceActive", reason="presence.on")
        self._presence_state = True
        return False

    @staticmethod
    def _is_invalid_llm_output(text: str) -> bool:
        if not text.strip():
            return True
        lowered = text.lower()
        forbidden = (
            "please provide the transcript",
            "no transcript provided",
            "there was no transcript",
            "i'm unable to correct",
            "transcript is blank",
            "it seems there was no input",
        )
        if any(phrase in lowered for phrase in forbidden):
            return True
        if lowered.strip() in {"[no_speech]", "[blank_audio]", "[silence]"}:
            return True
        return False

    @staticmethod
    def _segment_mean_abs_amplitude(pcm: bytes) -> float:
        if not pcm:
            return 0.0
        samples = array("h")
        samples.frombytes(pcm)
        if not samples:
            return 0.0
        total = 0
        for sample in samples:
            total += abs(sample)
        return total / len(samples)

    @staticmethod
    def _infer_sample_rate(headers: dict, content_type: Optional[str]) -> Optional[int]:
        keys = [
            "x-audio-sample-rate",
            "x-sample-rate",
            "sample-rate",
            "samplerate",
        ]
        for key in keys:
            if key in headers:
                try:
                    return int(str(headers[key]).strip())
                except ValueError:
                    continue
        if content_type:
            parts = content_type.split(";")
            for part in parts:
                if "=" in part:
                    name, value = part.split("=", 1)
                    if name.strip().lower() in {"rate", "samplerate"}:
                        try:
                            return int(value.strip())
                        except ValueError:
                            continue
        return None

    @staticmethod
    def _apply_gain(pcm: bytes, gain_db: float) -> bytes:
        if not pcm or gain_db == 0.0:
            return pcm
        factor = math.pow(10.0, gain_db / 20.0)
        samples = array("h", pcm)
        for i, sample in enumerate(samples):
            amplified = int(round(sample * factor))
            if amplified > 32767:
                amplified = 32767
            elif amplified < -32768:
                amplified = -32768
            samples[i] = amplified
        return samples.tobytes()

    @staticmethod
    def _resample_if_needed(pcm: bytes, src_rate: int, target_rate: int) -> tuple[bytes, int]:
        if target_rate <= 0 or src_rate == target_rate:
            return pcm, src_rate
        if target_rate > src_rate:
            raise ValueError("Upsampling is not supported for playback")

        samples = array("h", pcm)
        ratio = src_rate / target_rate
        target_length = max(1, int(len(samples) / ratio))
        resampled = array("h", [0] * target_length)
        for i in range(target_length):
            src_index = i * ratio
            left = int(math.floor(src_index))
            right = min(left + 1, len(samples) - 1)
            frac = src_index - left
            if right == left:
                value = samples[left]
            else:
                value = int(round(samples[left] + (samples[right] - samples[left]) * frac))
            resampled[i] = value
        return resampled.tobytes(), target_rate
