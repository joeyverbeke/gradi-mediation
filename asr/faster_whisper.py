"""Faster-Whisper integration for low-latency ASR."""

from __future__ import annotations

import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

try:
    from faster_whisper import WhisperModel  # type: ignore
except ImportError:  # pragma: no cover - import guard
    WhisperModel = None

from .whisper_cpp import TranscriptionResult


@dataclass(frozen=True)
class FasterWhisperConfig:
    """Runtime options for Faster-Whisper."""

    model_dir: Path
    device: str = "cuda"
    compute_type: str = "float16"
    language: Optional[str] = None
    beam_size: int = 1
    temperature: float = 0.0

    def __post_init__(self) -> None:
        if not self.model_dir.exists():
            raise FileNotFoundError(f"Faster-Whisper model directory not found: {self.model_dir}")


class FasterWhisperTranscriber:
    """Transcribe audio using the Faster-Whisper Python bindings."""

    def __init__(self, config: FasterWhisperConfig) -> None:
        self.config = config
        self._model: Optional[WhisperModel] = None
        self._model_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API

    def transcribe_pcm(self, pcm: bytes, *, sample_rate: int = 16_000) -> TranscriptionResult:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            temp_path = Path(tmp.name)
        try:
            self._write_wav(temp_path, pcm, sample_rate)
            return self.transcribe_file(temp_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def transcribe_file(self, audio_path: Path) -> TranscriptionResult:
        if not audio_path.exists():
            raise FileNotFoundError(audio_path)

        model = self._ensure_model()
        segments_iter, info = model.transcribe(
            str(audio_path),
            beam_size=self.config.beam_size,
            temperature=self.config.temperature,
            language=self.config.language,
        )

        segments: List[str] = []
        for segment in segments_iter:
            txt = (segment.text or "").strip()
            if txt:
                segments.append(txt)

        text = " ".join(segments).strip()
        metadata = {
            "language": info.language,
            "duration": info.duration,
            "transcription_latency": getattr(info, "transcription_duration", None),
        }
        return TranscriptionResult(text=text, segments=segments, raw_json=metadata, audio_path=audio_path)

    def transcribe_segments(self, segments: Iterable[bytes], *, sample_rate: int = 16_000) -> List[TranscriptionResult]:
        results: List[TranscriptionResult] = []
        for pcm in segments:
            results.append(self.transcribe_pcm(pcm, sample_rate=sample_rate))
        return results

    # ------------------------------------------------------------------
    # Helpers

    def _ensure_model(self) -> WhisperModel:
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is None:
                if WhisperModel is None:
                    raise ImportError(
                        "faster-whisper is not installed. Install it with `uv pip install faster-whisper`."
                    )
                self._model = WhisperModel(
                    str(self.config.model_dir),
                    device=self.config.device,
                    compute_type=self.config.compute_type,
                )
            return self._model

    @staticmethod
    def _write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm)
