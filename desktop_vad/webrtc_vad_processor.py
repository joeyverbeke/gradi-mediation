"""WebRTC-based voice activity detection helpers.

This module wraps `webrtcvad` and exposes a stream-oriented processor that emits
(start, end) timestamps for regions that likely contain speech. The processor is
configurable but keeps sensible defaults for 16 kHz mono PCM captured from the
ESP32-S3 microphones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

try:
    import webrtcvad  # type: ignore
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError(
        "webrtcvad is required for desktop_vad. Install it with `uv pip install webrtcvad`."
    ) from exc


@dataclass(frozen=True)
class VADConfig:
    """Runtime configuration for :class:`WebRTCVADProcessor`.

    Attributes:
        sample_rate: PCM sample rate in Hz. WebRTC VAD supports 8000, 16000,
            32000, and 48000.
        frame_duration_ms: Frame size in milliseconds. Supported values are
            10, 20, or 30 and must evenly divide ``padding_ms``.
        aggressiveness: VAD aggressiveness level (0..3). Higher values are more
            aggressive at filtering noise but may clip soft speech.
        start_trigger_frames: Number of consecutive speech frames required to
            mark a speech start. Helps avoid spurious triggers.
        stop_trigger_frames: Number of consecutive non-speech frames required to
            stop an active segment. Provides hangover to capture trailing speech.
    """

    sample_rate: int = 16_000
    frame_duration_ms: int = 30
    aggressiveness: int = 2
    start_trigger_frames: int = 3
    stop_trigger_frames: int = 30

    def __post_init__(self) -> None:
        if self.sample_rate not in (8000, 16000, 32000, 48000):
            raise ValueError("sample_rate must be one of 8000, 16000, 32000, 48000")
        if self.frame_duration_ms not in (10, 20, 30):
            raise ValueError("frame_duration_ms must be 10, 20, or 30")
        if not (0 <= self.aggressiveness <= 3):
            raise ValueError("aggressiveness must be between 0 and 3")
        if self.start_trigger_frames < 1:
            raise ValueError("start_trigger_frames must be >= 1")
        if self.stop_trigger_frames < 1:
            raise ValueError("stop_trigger_frames must be >= 1")


class WebRTCVADProcessor:
    """Stream processor that emits speech segment boundaries.

    Usage::

        vad = WebRTCVADProcessor()
        segments = vad.process(pcm_bytes)  # -> List[(start_sec, end_sec)]

    You can also pass PCM frames incrementally using :meth:`process_frames`.
    """

    def __init__(self, config: VADConfig | None = None) -> None:
        self.config = config or VADConfig()
        self._vad = webrtcvad.Vad(self.config.aggressiveness)
        self._frame_bytes = int(self.config.sample_rate * self.config.frame_duration_ms / 1000) * 2
        self._frame_duration_s = self.config.frame_duration_ms / 1000.0

    def _iter_frames(self, pcm: bytes) -> Iterable[bytes]:
        for offset in range(0, len(pcm) - self._frame_bytes + 1, self._frame_bytes):
            yield pcm[offset : offset + self._frame_bytes]

    def process(self, pcm: bytes) -> List[Tuple[float, float]]:
        """Return speech segments from a PCM byte stream."""

        frames = list(self._iter_frames(pcm))
        return self.process_frames(frames)

    def process_frames(self, frames: Sequence[bytes]) -> List[Tuple[float, float]]:
        """Return speech segments detected within the provided frames."""

        cfg = self.config
        segments: List[Tuple[float, float]] = []

        active = False
        start_frame = 0
        consecutive_speech = 0
        consecutive_silence = 0

        for idx, frame in enumerate(frames):
            is_speech = self._vad.is_speech(frame, cfg.sample_rate)

            if is_speech:
                consecutive_speech += 1
                consecutive_silence = 0
            else:
                consecutive_speech = 0
                consecutive_silence += 1

            if not active:
                if is_speech and consecutive_speech >= cfg.start_trigger_frames:
                    active = True
                    # Backdate the start to include the trigger frames
                    start_frame = idx - cfg.start_trigger_frames + 1
                    if start_frame < 0:
                        start_frame = 0
            else:
                if not is_speech and consecutive_silence >= cfg.stop_trigger_frames:
                    end_frame = idx - cfg.stop_trigger_frames + 1
                    if end_frame < start_frame:
                        end_frame = idx
                    segments.append(
                        (
                            start_frame * self._frame_duration_s,
                            end_frame * self._frame_duration_s,
                        )
                    )
                    active = False
                    consecutive_speech = 0
                    consecutive_silence = 0

        if active:
            segments.append(
                (
                    start_frame * self._frame_duration_s,
                    len(frames) * self._frame_duration_s,
                )
            )

        return merge_adjacent_segments(segments)


def merge_adjacent_segments(
    segments: Sequence[Tuple[float, float]],
    *,
    gap_threshold: float = 0.06,
    min_duration: float = 0.2,
) -> List[Tuple[float, float]]:
    """Merge segments separated by tiny gaps and drop very short activations."""

    if not segments:
        return []

    merged: List[Tuple[float, float]] = []
    cur_start, cur_end = segments[0]

    for start, end in segments[1:]:
        if start - cur_end <= gap_threshold:
            cur_end = max(cur_end, end)
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = start, end

    merged.append((cur_start, cur_end))
    return [seg for seg in merged if seg[1] - seg[0] >= min_duration]


def detect_voiced_segments(
    pcm: bytes,
    *,
    sample_rate: int = 16_000,
    frame_duration_ms: int = 30,
    aggressiveness: int = 2,
    start_trigger_frames: int = 3,
    stop_trigger_frames: int = 5,
    min_duration: float = 0.2,
) -> List[Tuple[float, float]]:
    """Convenience function that runs WebRTC VAD with the given parameters."""

    cfg = VADConfig(
        sample_rate=sample_rate,
        frame_duration_ms=frame_duration_ms,
        aggressiveness=aggressiveness,
        start_trigger_frames=start_trigger_frames,
        stop_trigger_frames=stop_trigger_frames,
    )
    processor = WebRTCVADProcessor(cfg)
    return processor.process(pcm)
