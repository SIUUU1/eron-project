from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ProviderTranscriptionError


_KNOWN_NON_SPEECH_HALLUCINATIONS = {
    "시청해주셔서감사합니다",
    "시청해주셔서고맙습니다",
    "구독과좋아요부탁드립니다",
}


def is_known_non_speech_hallucination(text: str) -> bool:
    """Reject only common subtitle boilerplate, not uncertain clinical speech."""
    normalized = "".join(character for character in text if character.isalnum()).lower()
    return normalized in _KNOWN_NON_SPEECH_HALLUCINATIONS


@dataclass(frozen=True)
class GroqWhisperConfig:
    api_key: str
    model: str = "whisper-large-v3-turbo"
    language: str | None = "ko"
    timeout_seconds: float = 120.0
    max_retries: int = 2

    @classmethod
    def from_env(cls) -> "GroqWhisperConfig":
        language = os.getenv("WHISPER_LANGUAGE", "ko").strip()
        return cls(
            api_key=os.getenv("GROQ_API_KEY", "").strip(),
            model=os.getenv(
                "GROQ_WHISPER_MODEL", "whisper-large-v3-turbo"
            ).strip(),
            language=language or None,
            timeout_seconds=float(os.getenv("GROQ_TIMEOUT_SECONDS", "120")),
            max_retries=int(os.getenv("GROQ_MAX_RETRIES", "2")),
        )


class GroqWhisperTranscriber:
    """Transcribe through Groq while preserving the API1 v1 result shape."""

    def __init__(
        self,
        config: GroqWhisperConfig | None = None,
        client: Any | None = None,
    ) -> None:
        self.config = config or GroqWhisperConfig.from_env()
        self._client = client

    @property
    def is_loaded(self) -> bool:
        return self._client is not None

    def _create_client(self) -> Any:
        if not self.config.api_key:
            raise RuntimeError("GROQ_API_KEY must be configured")
        from groq import Groq

        return Groq(
            api_key=self.config.api_key,
            timeout=self.config.timeout_seconds,
            max_retries=self.config.max_retries,
        )

    def preload(self) -> dict[str, Any]:
        if self._client is None:
            self._client = self._create_client()
        return self.status()

    def status(self) -> dict[str, Any]:
        return {
            "provider": "groq",
            "deployment_mode": "cloud_demo",
            "model": self.config.model,
            "active_device": "cloud" if self.is_loaded else None,
            "language": self.config.language or "auto",
            "loaded": self.is_loaded,
        }

    @staticmethod
    def _as_dict(response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            return response
        model_dump = getattr(response, "model_dump", None)
        if callable(model_dump):
            return model_dump()
        raise TypeError("Unsupported Groq transcription response")

    @staticmethod
    def _safe_error_code(error: Exception) -> str:
        status_code = getattr(error, "status_code", None)
        if status_code in {401, 403}:
            return "CLOUD_AUTH_FAILED"
        if status_code == 413:
            return "CLOUD_FILE_TOO_LARGE"
        if status_code == 429:
            return "CLOUD_RATE_LIMITED"
        if (
            status_code == 408
            or isinstance(error, TimeoutError)
            or "timeout" in type(error).__name__.lower()
        ):
            return "CLOUD_TIMEOUT"
        if isinstance(status_code, int) and status_code >= 500:
            return "CLOUD_UNAVAILABLE"
        return "CLOUD_REQUEST_FAILED"

    def transcribe(self, audio_path: Path) -> dict[str, Any]:
        client = self._client or self._create_client()
        self._client = client
        started_at = time.perf_counter()
        with audio_path.open("rb") as audio_file:
            try:
                response = client.audio.transcriptions.create(
                    file=(audio_path.name, audio_file.read()),
                    model=self.config.model,
                    language=self.config.language,
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                    temperature=0.0,
                )
            except Exception as error:
                raise ProviderTranscriptionError(
                    self._safe_error_code(error)
                ) from None

        payload = self._as_dict(response)
        segments: list[dict[str, Any]] = []
        for index, segment in enumerate(payload.get("segments") or [], start=1):
            text = str(segment.get("text", "")).strip()
            if not text or is_known_non_speech_hallucination(text):
                continue
            segments.append(
                {
                    "id": f"seg_{index:04d}",
                    "start": round(float(segment["start"]), 3),
                    "end": round(float(segment["end"]), 3),
                    "text": text,
                    "avg_logprob": (
                        round(float(segment["avg_logprob"]), 4)
                        if segment.get("avg_logprob") is not None
                        else None
                    ),
                    "no_speech_prob": (
                        round(float(segment["no_speech_prob"]), 4)
                        if segment.get("no_speech_prob") is not None
                        else None
                    ),
                }
            )
        duration = payload.get("duration")
        if duration is None and segments:
            duration = segments[-1]["end"]
        return {
            "text": " ".join(segment["text"] for segment in segments),
            "segments": segments,
            "language": payload.get("language") or self.config.language or "unknown",
            "language_probability": None,
            "duration_seconds": round(float(duration or 0.0), 3),
            "duration_after_vad_seconds": None,
            "processing": {
                "provider": "groq",
                "deployment_mode": "cloud_demo",
                "model": self.config.model,
                "device": "cloud",
                "compute_type": "remote",
                "language": self.config.language or "auto",
                "elapsed_seconds": round(time.perf_counter() - started_at, 3),
            },
        }
