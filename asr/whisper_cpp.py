"""Thin wrapper around whisper.cpp for offline ASR."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence


@dataclass(frozen=True)
class WhisperCppConfig:
    """Configuration for running whisper.cpp."""

    binary_path: Path
    model_path: Path
    language: str = "en"
    translate: bool = False
    extra_args: Sequence[str] = ()
    sample_rate: int = 16_000

    def __post_init__(self) -> None:
        resolved = self._resolve_binary_path(self.binary_path)
        object.__setattr__(self, "binary_path", resolved)
        if not self.model_path.exists():
            raise FileNotFoundError(f"whisper.cpp model not found at {self.model_path}")
        if self.sample_rate not in (8_000, 16_000, 24_000, 32_000, 44_100, 48_000):
            raise ValueError("sample_rate should match supported whisper.cpp rates")

    def _resolve_binary_path(self, candidate: Path) -> Path:
        candidate = candidate.expanduser().resolve()
        if candidate.is_file():
            return candidate

        # If folder was provided, check for whisper-cli (preferred) then main.
        common_names = (
            "whisper-cli",
            "build/bin/whisper-cli",
            "bin/whisper-cli",
            "main",
            "build/bin/main",
            "bin/main",
        )
        for name in common_names:
            probe = (candidate / name).resolve()
            if probe.exists() and probe.is_file():
                return probe

        raise FileNotFoundError(
            f"Could not find whisper.cpp executable. Checked {candidate} and common subpaths"
        )


@dataclass(frozen=True)
class TranscriptionResult:
    """Container for transcription outputs."""

    text: str
    segments: List[str]
    raw_json: Optional[dict]
    audio_path: Path


class WhisperCppTranscriber:
    """Runs whisper.cpp against PCM buffers or audio files."""

    def __init__(self, config: WhisperCppConfig) -> None:
        self.config = config
        shutil.which(str(config.binary_path))  # ensure string path resolution

    def transcribe_pcm(self, pcm: bytes, *, sample_rate: Optional[int] = None) -> TranscriptionResult:
        sr = sample_rate or self.config.sample_rate
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            self._write_wav(tmp_path, pcm, sr)
            return self.transcribe_file(tmp_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def transcribe_file(self, audio_path: Path) -> TranscriptionResult:
        if not audio_path.exists():
            raise FileNotFoundError(audio_path)

        with tempfile.TemporaryDirectory() as td:
            out_prefix = Path(td) / "out"
            cmd = self._build_command(audio_path, out_prefix)
            try:
                subprocess.run(cmd, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    "whisper.cpp failed with code "
                    f"{exc.returncode}:\nSTDOUT:\n{exc.stdout}\nSTDERR:\n{exc.stderr}"
                ) from exc
            json_path = out_prefix.with_suffix(".json")
            data = self._load_json(json_path)
            text = self._extract_text(data)
            segments = self._extract_segments(data)
            return TranscriptionResult(text=text, segments=segments, raw_json=data, audio_path=audio_path)

    def transcribe_segments(self, segments: Iterable[bytes], *, sample_rate: Optional[int] = None) -> List[TranscriptionResult]:
        results: List[TranscriptionResult] = []
        for pcm in segments:
            results.append(self.transcribe_pcm(pcm, sample_rate=sample_rate))
        return results

    def _build_command(self, audio_path: Path, out_prefix: Path) -> List[str]:
        cfg = self.config
        cmd = [str(cfg.binary_path), "-m", str(cfg.model_path), "-f", str(audio_path)]
        cmd.extend(["--language", cfg.language])
        cmd.extend(["--output-json", "--output-file", str(out_prefix)])
        if cfg.translate:
            cmd.append("--translate")
        if cfg.extra_args:
            cmd.extend(cfg.extra_args)
        return cmd

    def _write_wav(self, path: Path, pcm: bytes, sample_rate: int) -> None:
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm)

    def _load_json(self, json_path: Path) -> Optional[dict]:
        if not json_path.exists():
            raise FileNotFoundError(f"whisper.cpp output JSON missing at {json_path}")
        try:
            return json.loads(json_path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to parse whisper.cpp JSON: {json_path}") from exc

    def _extract_text(self, data: Optional[dict]) -> str:
        if isinstance(data, dict):
            if "text" in data and isinstance(data["text"], str):
                return data["text"].strip()
            if "transcription" in data and isinstance(data["transcription"], list):
                return "".join(item.get("text", "") for item in data["transcription"]).strip()
        return ""

    def _extract_segments(self, data: Optional[dict]) -> List[str]:
        if not isinstance(data, dict):
            return []
        segments: List[str] = []
        if isinstance(data.get("transcription"), list):
            for item in data["transcription"]:
                txt = item.get("text")
                if isinstance(txt, str):
                    segments.append(txt.strip())
        elif "text" in data:
            segments.append(str(data["text"]).strip())
        return segments
