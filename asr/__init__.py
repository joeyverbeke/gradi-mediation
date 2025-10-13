"""ASR orchestration utilities."""

from pathlib import Path
from typing import Protocol

from .whisper_cpp import TranscriptionResult, WhisperCppConfig, WhisperCppTranscriber
from .faster_whisper import FasterWhisperConfig, FasterWhisperTranscriber
from .vosk_transcriber import VoskConfig, VoskTranscriber


class ASRTranscriber(Protocol):
    """Protocol shared by ASR backends."""

    def transcribe_pcm(self, pcm: bytes, *, sample_rate: int = 16_000) -> TranscriptionResult:  # pragma: no cover - structural
        ...

    def transcribe_file(self, audio_path: Path) -> TranscriptionResult:  # pragma: no cover - structural
        ...

__all__ = [
    "TranscriptionResult",
    "WhisperCppConfig",
    "WhisperCppTranscriber",
    "FasterWhisperConfig",
    "FasterWhisperTranscriber",
    "VoskConfig",
    "VoskTranscriber",
    "ASRTranscriber",
]
