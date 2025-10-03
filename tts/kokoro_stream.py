"""Streaming client for Kokoro-FastAPI's OpenAI-compatible TTS endpoint."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, Optional

try:
    import requests
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError(
        "The requests package is required for the Kokoro TTS client. Install it with `uv pip install requests`."
    ) from exc


_ACCEPT_HEADER_MAP = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "opus": "audio/ogg",
    "pcm": "application/octet-stream",
}


@dataclass(frozen=True)
class KokoroConfig:
    """Runtime configuration for streaming against Kokoro-FastAPI."""

    base_url: str = "http://127.0.0.1:8880/v1"
    endpoint: str = "/audio/speech"
    model: str = "kokoro"
    voice: Optional[str] = None
    response_format: str = "wav"
    speed: Optional[float] = None
    stream_chunk_bytes: int = 32_768
    connect_timeout: float = 5.0
    read_timeout: float = 60.0
    extra_payload: Dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ValueError("base_url must be provided")
        normalized = self.base_url.rstrip("/")
        object.__setattr__(self, "base_url", normalized)

        if not self.endpoint:
            raise ValueError("endpoint must be provided")
        if not self.endpoint.startswith("/"):
            object.__setattr__(self, "endpoint", f"/{self.endpoint}")

        if not self.model:
            raise ValueError("model must be provided")
        if self.stream_chunk_bytes <= 0:
            raise ValueError("stream_chunk_bytes must be positive")
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise ValueError("timeouts must be positive")

    def build_payload(self, text: str) -> Dict[str, object]:
        if not text or not text.strip():
            raise ValueError("text to synthesise must be non-empty")
        payload: Dict[str, object] = dict(self.extra_payload)
        payload.setdefault("model", self.model)
        payload["input"] = text
        if self.voice:
            payload.setdefault("voice", self.voice)
        if self.response_format:
            payload.setdefault("response_format", self.response_format)
        if self.speed is not None:
            payload.setdefault("speed", self.speed)
        return payload

    def build_url(self) -> str:
        return f"{self.base_url}{self.endpoint}"

    def accept_header(self) -> str:
        return _ACCEPT_HEADER_MAP.get(self.response_format.lower(), "*/*")


@dataclass(frozen=True)
class SynthesisChunk:
    """Represents a chunk of streamed audio or the terminal signal."""

    data: bytes
    sequence: int
    is_last: bool
    total_bytes: int
    first_chunk_latency_s: Optional[float] = None
    elapsed_s: Optional[float] = None
    content_type: Optional[str] = None
    headers: Dict[str, str] | None = None


@dataclass(frozen=True)
class SynthesisMetadata:
    """Summary of the synthesis stream once complete."""

    text: str
    output_path: Path
    bytes_received: int
    chunk_count: int
    first_chunk_latency_s: Optional[float]
    total_elapsed_s: float
    content_type: Optional[str]


class KokoroStreamer:
    """Client for streaming audio from Kokoro-FastAPI."""

    def __init__(self, config: KokoroConfig) -> None:
        self.config = config
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "KokoroStreamer":  # pragma: no cover - convenience
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - convenience
        self.close()

    def stream_synthesis(self, text: str) -> Iterator[SynthesisChunk]:
        """Yield audio chunks as soon as Kokoro produces them."""

        payload = self.config.build_payload(text)
        url = self.config.build_url()
        start_time = time.monotonic()
        response = self._session.post(
            url,
            json=payload,
            headers={"accept": self.config.accept_header()},
            stream=True,
            timeout=(self.config.connect_timeout, self.config.read_timeout),
        )
        try:
            if response.status_code >= 400:
                detail = self._extract_error_detail(response)
                raise RuntimeError(
                    f"Kokoro TTS request failed with status {response.status_code}: {detail}"
                )

            content_type = response.headers.get("Content-Type")
            if content_type and "application/json" in content_type.lower():
                detail = self._extract_error_detail(response)
                raise RuntimeError(f"Kokoro TTS returned JSON payload instead of audio: {detail}")

            headers = {k.lower(): v for k, v in response.headers.items()}

            sequence = 0
            total_bytes = 0
            first_chunk_latency: Optional[float] = None

            for raw_chunk in response.iter_content(chunk_size=self.config.stream_chunk_bytes):
                if not raw_chunk:
                    continue
                sequence += 1
                total_bytes += len(raw_chunk)
                now = time.monotonic()
                if first_chunk_latency is None:
                    first_chunk_latency = now - start_time
                yield SynthesisChunk(
                    data=raw_chunk,
                    sequence=sequence,
                    is_last=False,
                    total_bytes=total_bytes,
                    first_chunk_latency_s=first_chunk_latency if sequence == 1 else None,
                    content_type=content_type,
                    headers=headers,
                )

            elapsed = time.monotonic() - start_time
            yield SynthesisChunk(
                data=b"",
                sequence=sequence + 1,
                is_last=True,
                total_bytes=total_bytes,
                first_chunk_latency_s=first_chunk_latency,
                elapsed_s=elapsed,
                content_type=content_type,
                headers=headers,
            )
        finally:
            response.close()

    def stream_to_file(self, text: str, output_path: Path) -> SynthesisMetadata:
        """Stream audio directly into ``output_path`` and collect timing info."""

        output_path.parent.mkdir(parents=True, exist_ok=True)

        chunk_count = 0
        total_bytes = 0
        first_chunk_latency: Optional[float] = None
        content_type: Optional[str] = None
        final_chunk: Optional[SynthesisChunk] = None

        with output_path.open("wb") as handle:
            for chunk in self.stream_synthesis(text):
                if chunk.is_last:
                    final_chunk = chunk
                    break
                handle.write(chunk.data)
                chunk_count += 1
                total_bytes = chunk.total_bytes
                if chunk.first_chunk_latency_s is not None and first_chunk_latency is None:
                    first_chunk_latency = chunk.first_chunk_latency_s
                if content_type is None:
                    content_type = chunk.content_type

        if final_chunk is None:
            raise RuntimeError("Kokoro stream ended without completion signal")

        total_bytes = final_chunk.total_bytes or total_bytes
        if first_chunk_latency is None:
            first_chunk_latency = final_chunk.first_chunk_latency_s
        if content_type is None:
            content_type = final_chunk.content_type

        elapsed = final_chunk.elapsed_s if final_chunk.elapsed_s is not None else 0.0

        return SynthesisMetadata(
            text=text,
            output_path=output_path,
            bytes_received=total_bytes,
            chunk_count=chunk_count,
            first_chunk_latency_s=first_chunk_latency,
            total_elapsed_s=elapsed,
            content_type=content_type,
        )

    def _extract_error_detail(self, response: requests.Response) -> str:
        try:
            data = response.json()
            return str(data)
        except ValueError:
            text = response.text
            return text[:400]

