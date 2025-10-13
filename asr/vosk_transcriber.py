"""Vosk-based automatic speech recognition backend."""

from __future__ import annotations

import json
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

try:  # pragma: no cover - optional dependency guard
    from vosk import KaldiRecognizer, Model  # type: ignore
except ImportError:  # pragma: no cover - handled at runtime
    KaldiRecognizer = None
    Model = None

from .whisper_cpp import TranscriptionResult


@dataclass(frozen=True)
class VoskConfig:
    """Runtime configuration for the Vosk recogniser."""

    model_path: Path
    sample_rate: int = 16_000
    enable_words: bool = True
    enable_partial_results: bool = False
    grammar: Optional[Sequence[str]] = field(default=None)

    def __post_init__(self) -> None:
        resolved = self._resolve_model_path(self.model_path)
        object.__setattr__(self, "model_path", resolved)
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be a positive integer")

    @staticmethod
    def _resolve_model_path(path: Path) -> Path:
        candidate = path.expanduser().resolve()
        if not candidate.exists():
            raise FileNotFoundError(f"Vosk model path not found: {candidate}")
        if candidate.is_file():
            raise ValueError(f"Vosk model path must be a directory: {candidate}")

        def looks_like_model(root: Path) -> bool:
            return (root / "am").exists() and (root / "conf").exists()

        if looks_like_model(candidate):
            return candidate

        subdirs = [p for p in candidate.iterdir() if p.is_dir()]
        if len(subdirs) == 1 and looks_like_model(subdirs[0]):
            return subdirs[0]

        raise FileNotFoundError(
            f"Vosk model directory missing expected files (am/conf): {candidate}"
        )


class VoskTranscriber:
    """ASR adapter that uses the Vosk Python bindings."""

    def __init__(self, config: VoskConfig) -> None:
        self.config = config
        self._model: Optional[Model] = None
        self._model_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API

    def transcribe_pcm(self, pcm: bytes, *, sample_rate: Optional[int] = None) -> TranscriptionResult:
        sr = sample_rate or self.config.sample_rate
        if sr != self.config.sample_rate:
            raise ValueError(
                f"VoskTranscriber expected sample_rate={self.config.sample_rate}, received {sr}"
            )
        recognizer = self._build_recognizer(sr)
        result_data = self._run_recognition(recognizer, pcm)
        return self._build_result(result_data, Path("<pcm>"))

    def transcribe_file(self, audio_path: Path) -> TranscriptionResult:
        if not audio_path.exists():
            raise FileNotFoundError(audio_path)
        with wave.open(str(audio_path), "rb") as wav_file:
            nchannels = wav_file.getnchannels()
            sampwidth = wav_file.getsampwidth()
            framerate = wav_file.getframerate()
            if sampwidth != 2:
                raise ValueError(
                    f"Vosk expects 16-bit PCM. Found sample width {sampwidth * 8} bits in {audio_path}"
                )
            if nchannels != 1:
                raise ValueError(
                    f"Vosk expects mono audio. Found {nchannels} channels in {audio_path}"
                )
            if framerate != self.config.sample_rate:
                raise ValueError(
                    f"VoskTranscriber configured for {self.config.sample_rate} Hz but file uses {framerate} Hz"
                )
            pcm = wav_file.readframes(wav_file.getnframes())
        recognizer = self._build_recognizer(framerate)
        result_data = self._run_recognition(recognizer, pcm)
        return self._build_result(result_data, audio_path)

    def transcribe_segments(self, segments: Iterable[bytes], *, sample_rate: Optional[int] = None) -> List[TranscriptionResult]:
        results: List[TranscriptionResult] = []
        for pcm in segments:
            results.append(self.transcribe_pcm(pcm, sample_rate=sample_rate))
        return results

    # ------------------------------------------------------------------
    # Helpers

    def _ensure_model(self) -> Model:
        if self._model is not None:
            return self._model
        if Model is None:
            raise ImportError("vosk is not installed. Install it with `uv pip install vosk`.")
        with self._model_lock:
            if self._model is None:
                self._model = Model(str(self.config.model_path))
            return self._model

    def _build_recognizer(self, sample_rate: int) -> KaldiRecognizer:
        model = self._ensure_model()
        if KaldiRecognizer is None:  # pragma: no cover - defensive
            raise ImportError("vosk is not installed. Install it with `uv pip install vosk`.")
        if self.config.grammar is not None:
            recognizer = KaldiRecognizer(model, sample_rate, json.dumps(list(self.config.grammar)))
        else:
            recognizer = KaldiRecognizer(model, sample_rate)
        if self.config.enable_words and hasattr(recognizer, "SetWords"):
            recognizer.SetWords(True)
        if self.config.enable_partial_results and hasattr(recognizer, "SetPartialWords"):
            recognizer.SetPartialWords(True)
        return recognizer

    @staticmethod
    def _run_recognition(recognizer: KaldiRecognizer, pcm: bytes) -> dict:
        recognizer.AcceptWaveform(pcm)
        final_json = recognizer.FinalResult()
        try:
            return json.loads(final_json) if final_json else {"text": ""}
        except json.JSONDecodeError as exc:  # pragma: no cover - corrupt output
            raise ValueError("Failed to decode Vosk JSON result") from exc

    @staticmethod
    def _extract_text_and_segments(data: dict) -> tuple[str, List[str]]:
        text = (data.get("text") or "").strip()
        segments: List[str] = []
        words = data.get("result")
        if isinstance(words, list):
            accumulated: List[str] = []
            for word_entry in words:
                word = word_entry.get("word")
                if isinstance(word, str):
                    accumulated.append(word)
            if accumulated:
                segments.append(" ".join(accumulated).strip())
        if not segments and text:
            segments.append(text)
        return text, segments

    def _build_result(self, data: dict, audio_path: Path) -> TranscriptionResult:
        text, segments = self._extract_text_and_segments(data)
        return TranscriptionResult(
            text=text,
            segments=segments,
            raw_json=data,
            audio_path=audio_path,
        )


__all__ = ["VoskConfig", "VoskTranscriber"]
