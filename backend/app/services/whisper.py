"""Client for the internal asynchronous Whisper transcription service."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx2


_JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class InvalidWhisperResponseError(RuntimeError):
    """The STT service returned a response that violates its public contract."""


class WhisperTimeoutError(RuntimeError):
    """The STT job did not complete within the configured deadline."""


class WhisperUnavailableError(RuntimeError):
    """The STT service could not accept or serve the transcription job."""


class WhisperTranscriptionFailedError(RuntimeError):
    """The STT service completed the job in a failed terminal state."""


class WhisperInvalidAudioError(RuntimeError):
    """The STT service rejected the supplied media as invalid or unsupported."""


class WhisperAudioTooLargeError(RuntimeError):
    """The STT service rejected the supplied media because of its size."""


class WhisperClient:
    """Submit audio, wait for completion, and return the Whisper JSON result."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        poll_interval_seconds: float,
        http_client: httpx2.AsyncClient,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = max(timeout_seconds, 0.1)
        self._poll_interval_seconds = max(poll_interval_seconds, 0.0)
        self._http_client = http_client

    async def transcribe(
        self,
        *,
        filename: str,
        content_type: str | None,
        content: bytes,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if request_id:
            headers["X-Request-ID"] = request_id

        deadline = time.monotonic() + self._timeout_seconds
        try:
            submitted = await self._http_client.post(
                f"{self._base_url}/v1/transcriptions",
                headers=headers,
                files={
                    "audio": (
                        filename,
                        content,
                        content_type or "application/octet-stream",
                    )
                },
                timeout=self._remaining(deadline),
            )
        except httpx2.TimeoutException as exc:
            raise WhisperTimeoutError from exc
        except httpx2.RequestError as exc:
            raise WhisperUnavailableError from exc

        if submitted.status_code == 413:
            raise WhisperAudioTooLargeError
        if submitted.status_code in {400, 415, 422}:
            raise WhisperInvalidAudioError
        if submitted.status_code != 202:
            raise WhisperUnavailableError(
                f"STT submission returned HTTP {submitted.status_code}"
            )
        submission = self._json_object(submitted)
        job_id = submission.get("id")
        if not isinstance(job_id, str) or not _JOB_ID_PATTERN.fullmatch(job_id):
            raise InvalidWhisperResponseError("STT response contains an invalid job id")

        await self._wait_for_completion(job_id, headers, deadline)
        return await self._fetch_result(job_id, headers, deadline)

    async def _wait_for_completion(
        self,
        job_id: str,
        headers: dict[str, str],
        deadline: float,
    ) -> None:
        status_url = f"{self._base_url}/v1/transcriptions/{job_id}"
        while True:
            try:
                response = await self._http_client.get(
                    status_url,
                    headers=headers,
                    timeout=self._remaining(deadline),
                )
            except httpx2.TimeoutException as exc:
                raise WhisperTimeoutError from exc
            except httpx2.RequestError as exc:
                raise WhisperUnavailableError from exc

            if response.status_code != 200:
                raise WhisperUnavailableError(
                    f"STT status returned HTTP {response.status_code}"
                )
            status_payload = self._json_object(response)
            status = status_payload.get("status")
            if status == "completed":
                return
            if status in {"failed", "cancelled"}:
                raise WhisperTranscriptionFailedError
            if status not in {"queued", "processing"}:
                raise InvalidWhisperResponseError("STT response contains an invalid status")

            remaining = self._remaining(deadline)
            if self._poll_interval_seconds:
                await asyncio.sleep(min(self._poll_interval_seconds, remaining))

    async def _fetch_result(
        self,
        job_id: str,
        headers: dict[str, str],
        deadline: float,
    ) -> dict[str, Any]:
        try:
            response = await self._http_client.get(
                f"{self._base_url}/v1/transcriptions/{job_id}/result",
                headers=headers,
                timeout=self._remaining(deadline),
            )
        except httpx2.TimeoutException as exc:
            raise WhisperTimeoutError from exc
        except httpx2.RequestError as exc:
            raise WhisperUnavailableError from exc

        if response.status_code != 200:
            raise WhisperUnavailableError(
                f"STT result returned HTTP {response.status_code}"
            )
        result = self._json_object(response)
        if not isinstance(result.get("segments"), list):
            raise InvalidWhisperResponseError("STT result has no segments array")
        return result

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WhisperTimeoutError
        return remaining

    @staticmethod
    def _json_object(response: httpx2.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidWhisperResponseError("STT response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise InvalidWhisperResponseError("STT response is not a JSON object")
        return payload
