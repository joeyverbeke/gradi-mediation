"""Client helpers for running LLM transforms via vLLM's OpenAI-compatible API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence

try:
    import requests
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError(
        "The requests package is required for the vLLM client. Install it with `uv pip install requests`."
    ) from exc


@dataclass(frozen=True)
class VLLMConfig:
    """Configuration for the :class:`VLLMTransformer`."""

    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = "hugging-quants/Meta-Llama-3.1-8B-Instruct-GPTQ-INT4"
    temperature: float = 0.2
    top_p: float = 0.9
    max_tokens: int = 128
    timeout: float = 30.0
    system_prompt: str = (
        "You perfect automatic speech recognition transcripts."
        " Return only the corrected transcript with no explanations,"
        " headers, or meta commentary. If the transcript is blank or contains"
        " only noise markers, respond with the token [NO_SPEECH]."
    )
    stop: Sequence[str] = ()
    user_prompt_template: str = (
        "Rewrite the transcript into clean, fluent text in the same language."
        " Do not add new information or commentary. Output the corrected text only."
        " If the transcript is blank, non-speech, or noise markers such as"
        " [BLANK_AUDIO], reply with [NO_SPEECH].\n\nTranscript:\n{transcript}\n\nCorrected text:"
    )

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ValueError("base_url must be provided")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        normalized = self.base_url.rstrip("/")
        object.__setattr__(self, "base_url", normalized)


@dataclass(frozen=True)
class TransformResult:
    """Container for LLM transform outputs."""

    input_text: str
    output_text: str
    raw_response: Dict[str, object] = field(default_factory=dict)


class VLLMTransformer:
    """Minimal client for vLLM's OpenAI-compatible chat completions endpoint."""

    def __init__(self, config: VLLMConfig) -> None:
        self.config = config
        self._session = requests.Session()

    def transform(self, text: str) -> TransformResult:
        """Return the perfected variant of ``text``."""

        payload = self._build_payload(text)
        response = self._session.post(
            f"{self.config.base_url}/chat/completions",
            json=payload,
            timeout=self.config.timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"vLLM request failed with {response.status_code}: {response.text.strip()}"
            )
        data: Dict[str, object] = response.json()
        content = self._extract_content(data)
        if not content:
            raise RuntimeError(
                "vLLM response did not include completion content."
            )
        cleaned = content.strip()
        # Guardrail: ensure we do not exceed a loose character cap.
        max_chars = int(self.config.max_tokens * 4.2)
        if len(cleaned) > max_chars:
            cleaned = cleaned[:max_chars].rstrip()
        return TransformResult(input_text=text, output_text=cleaned, raw_response=data)

    def transform_batch(self, texts: Iterable[str]) -> List[TransformResult]:
        """Transform an iterable of transcripts sequentially."""

        results: List[TransformResult] = []
        for text in texts:
            results.append(self.transform(text))
        return results

    def close(self) -> None:
        """Close the underlying HTTP session."""

        self._session.close()

    def _build_payload(self, transcript: str) -> Dict[str, object]:
        cfg = self.config
        max_chars = int(cfg.max_tokens * 4.2)
        user_prompt = cfg.user_prompt_template.format(
            transcript=transcript.strip(),
            max_chars=max_chars,
        )
        payload: Dict[str, object] = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": cfg.system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": cfg.temperature,
            "top_p": cfg.top_p,
            "max_tokens": cfg.max_tokens,
        }
        if cfg.stop:
            payload["stop"] = list(cfg.stop)
        return payload

    def _extract_content(self, data: Dict[str, object]) -> str:
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0]
            if isinstance(choice, dict):
                message = choice.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content
                content = choice.get("text")
                if isinstance(content, str):
                    return content
        return ""

    def __enter__(self) -> "VLLMTransformer":  # pragma: no cover - convenience
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - convenience
        self.close()
