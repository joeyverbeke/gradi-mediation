"""Incremental VAD stream helper that emits speech events from continuous PCM."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

import webrtcvad  # type: ignore

from desktop_vad import VADConfig

BYTES_PER_SAMPLE = 2


@dataclass(frozen=True)
class SpeechStartEvent:
    """Event indicating VAD has latched onto speech."""

    start_time: float
    start_byte: int


@dataclass(frozen=True)
class SpeechSegment:
    """Completed speech segment cut from the rolling PCM buffer."""

    start_time: float
    end_time: float
    pcm: bytes


class VADStream:
    """Stateful wrapper around WebRTC VAD for continuous streams."""

    def __init__(
        self,
        config: VADConfig,
        *,
        preroll_frames: int = 2,
    ) -> None:
        self.config = config
        self._vad = webrtcvad.Vad(config.aggressiveness)
        self._frame_bytes = int(config.sample_rate * config.frame_duration_ms / 1000) * BYTES_PER_SAMPLE
        self._frame_duration_s = config.frame_duration_ms / 1000.0
        self._preroll_frames = max(0, preroll_frames)

        self._buffer = bytearray()
        self._processed_bytes = 0
        self._cursor = 0

        self._active = False
        self._start_frame: int = 0
        self._speech_run = 0
        self._silence_run = 0

    def add_audio(self, pcm: bytes) -> Sequence[object]:
        """Process PCM bytes and return any speech events produced."""

        if not pcm:
            return ()

        self._buffer.extend(pcm)
        events: List[object] = []

        while self._cursor + self._frame_bytes <= len(self._buffer):
            frame = self._buffer[self._cursor : self._cursor + self._frame_bytes]
            frame_index = (self._processed_bytes + self._cursor) // self._frame_bytes

            is_speech = self._vad.is_speech(frame, self.config.sample_rate)

            if is_speech:
                self._speech_run += 1
                self._silence_run = 0
            else:
                self._speech_run = 0
                self._silence_run += 1

            if not self._active:
                if is_speech and self._speech_run >= self.config.start_trigger_frames:
                    self._active = True
                    tentative_start = frame_index - self.config.start_trigger_frames + 1
                    start_frame = max(0, tentative_start - self._preroll_frames)
                    self._start_frame = start_frame
                    start_byte = start_frame * self._frame_bytes
                    start_time = start_frame * self._frame_duration_s
                    events.append(SpeechStartEvent(start_time=start_time, start_byte=start_byte))
            else:
                if not is_speech and self._silence_run >= self.config.stop_trigger_frames:
                    end_frame = frame_index - self.config.stop_trigger_frames + 1
                    if end_frame < self._start_frame:
                        end_frame = frame_index
                    end_byte = end_frame * self._frame_bytes
                    start_byte = self._start_frame * self._frame_bytes
                    events.append(self._slice_segment(start_byte, end_byte))
                    self._reset_after_segment(end_byte)

            self._cursor += self._frame_bytes

        # Prevent cursor from growing unbounded during long idle periods.
        max_buffer = self._frame_bytes * 100  # ~3 seconds at 30 ms frames
        if len(self._buffer) > max_buffer:
            trim = len(self._buffer) - max_buffer
            del self._buffer[:trim]
            self._processed_bytes += trim
            self._cursor = max(0, self._cursor - trim)

        return events

    def _slice_segment(self, start_byte: int, end_byte: int) -> SpeechSegment:
        start_rel = start_byte - self._processed_bytes
        end_rel = end_byte - self._processed_bytes
        if start_rel < 0:
            start_rel = 0
        if end_rel > len(self._buffer):
            end_rel = len(self._buffer)
        pcm = bytes(self._buffer[start_rel:end_rel])
        start_time = (start_byte / BYTES_PER_SAMPLE) / self.config.sample_rate
        end_time = (end_byte / BYTES_PER_SAMPLE) / self.config.sample_rate
        return SpeechSegment(start_time=start_time, end_time=end_time, pcm=pcm)

    def _reset_after_segment(self, end_byte: int) -> None:
        end_rel = end_byte - self._processed_bytes
        if end_rel < 0:
            end_rel = 0
        del self._buffer[:end_rel]
        self._processed_bytes += end_rel
        self._cursor = max(0, self._cursor - end_rel)
        self._active = False
        self._speech_run = 0
        self._silence_run = 0
        self._start_frame = 0

