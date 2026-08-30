"""Public endpoint for generating a reviewable emergency-record draft."""

from __future__ import annotations

from collections.abc import AsyncIterator
import json

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import JSONResponse
import httpx2
from pydantic import ValidationError

from app.core.config import settings
from app.schemas.clinical_record import WhisperDraftRequest
from app.services.clinicalnlp import (
    ClinicalNlpClient,
    ClinicalNlpTimeoutError,
    ClinicalNlpUnavailableError,
    InvalidClinicalNlpResponseError,
    InvalidWhisperPayloadError,
)
from app.services.whisper import (
    InvalidWhisperResponseError,
    WhisperAudioTooLargeError,
    WhisperClient,
    WhisperInvalidAudioError,
    WhisperTimeoutError,
    WhisperTranscriptionFailedError,
    WhisperUnavailableError,
)


router = APIRouter(prefix="/api/clinical-records", tags=["clinical-records"])
MAX_AUDIO_BYTES = 25 * 1024 * 1024


async def get_clinicalnlp_client() -> AsyncIterator[ClinicalNlpClient | None]:
    if not settings.record_ai_url:
        yield None
        return
    async with httpx2.AsyncClient() as http_client:
        yield ClinicalNlpClient(
            base_url=settings.record_ai_url,
            timeout_seconds=settings.clinical_record_ai_timeout_seconds,
            http_client=http_client,
        )


async def get_whisper_client() -> AsyncIterator[WhisperClient | None]:
    if not settings.stt_url:
        yield None
        return
    async with httpx2.AsyncClient() as http_client:
        yield WhisperClient(
            base_url=settings.stt_url,
            timeout_seconds=settings.stt_timeout_seconds,
            poll_interval_seconds=settings.stt_poll_interval_seconds,
            http_client=http_client,
        )


async def _generate_draft(
    payload: dict,
    client: ClinicalNlpClient,
) -> dict | JSONResponse:
    try:
        return await client.create_draft(payload)
    except InvalidWhisperPayloadError:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_whisper_payload"},
        )
    except InvalidClinicalNlpResponseError:
        return JSONResponse(
            status_code=502,
            content={"error": "invalid_clinicalnlp_response"},
        )
    except (ClinicalNlpTimeoutError, httpx2.TimeoutException):
        return JSONResponse(
            status_code=504,
            content={"error": "clinicalnlp_timeout"},
        )
    except (ClinicalNlpUnavailableError, httpx2.RequestError):
        return JSONResponse(
            status_code=503,
            content={"error": "clinicalnlp_unavailable"},
        )


@router.post("/draft", response_model=None)
async def create_clinical_record_draft(
    request: Request,
    client: ClinicalNlpClient | None = Depends(get_clinicalnlp_client),
) -> dict | JSONResponse:
    try:
        payload = await request.json()
        WhisperDraftRequest.model_validate(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_whisper_payload"},
        )
    if client is None:
        return JSONResponse(
            status_code=503,
            content={"error": "clinicalnlp_unavailable"},
        )
    return await _generate_draft(payload, client)


@router.post("/transcribe", response_model=None)
async def transcribe_clinical_record_audio(
    request: Request,
    audio: UploadFile = File(...),
    whisper_client: WhisperClient | None = Depends(get_whisper_client),
) -> dict | JSONResponse:
    if whisper_client is None:
        return JSONResponse(
            status_code=503,
            content={"error": "stt_unavailable"},
        )

    content = await audio.read(MAX_AUDIO_BYTES + 1)
    if not content:
        return JSONResponse(
            status_code=400,
            content={"error": "empty_audio"},
        )
    if len(content) > MAX_AUDIO_BYTES:
        return JSONResponse(
            status_code=413,
            content={"error": "audio_too_large"},
        )

    try:
        payload = await whisper_client.transcribe(
            filename=audio.filename or "audio",
            content_type=audio.content_type,
            content=content,
            request_id=request.headers.get("X-Request-ID"),
        )
        WhisperDraftRequest.model_validate(payload)
    except ValidationError:
        return JSONResponse(
            status_code=502,
            content={"error": "invalid_stt_response"},
        )
    except WhisperInvalidAudioError:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_audio"},
        )
    except WhisperAudioTooLargeError:
        return JSONResponse(
            status_code=413,
            content={"error": "audio_too_large"},
        )
    except InvalidWhisperResponseError:
        return JSONResponse(
            status_code=502,
            content={"error": "invalid_stt_response"},
        )
    except WhisperTimeoutError:
        return JSONResponse(
            status_code=504,
            content={"error": "stt_timeout"},
        )
    except WhisperTranscriptionFailedError:
        return JSONResponse(
            status_code=502,
            content={"error": "stt_transcription_failed"},
        )
    except WhisperUnavailableError:
        return JSONResponse(
            status_code=503,
            content={"error": "stt_unavailable"},
        )

    return payload


@router.post("/draft/audio", response_model=None)
async def create_clinical_record_draft_from_audio(
    request: Request,
    audio: UploadFile = File(...),
    whisper_client: WhisperClient | None = Depends(get_whisper_client),
    clinical_client: ClinicalNlpClient | None = Depends(get_clinicalnlp_client),
) -> dict | JSONResponse:
    if whisper_client is None:
        return JSONResponse(
            status_code=503,
            content={"error": "stt_unavailable"},
        )
    if clinical_client is None:
        return JSONResponse(
            status_code=503,
            content={"error": "clinicalnlp_unavailable"},
        )

    content = await audio.read(MAX_AUDIO_BYTES + 1)
    if not content:
        return JSONResponse(
            status_code=400,
            content={"error": "empty_audio"},
        )
    if len(content) > MAX_AUDIO_BYTES:
        return JSONResponse(
            status_code=413,
            content={"error": "audio_too_large"},
        )

    try:
        payload = await whisper_client.transcribe(
            filename=audio.filename or "audio",
            content_type=audio.content_type,
            content=content,
            request_id=request.headers.get("X-Request-ID"),
        )
        WhisperDraftRequest.model_validate(payload)
    except ValidationError:
        return JSONResponse(
            status_code=502,
            content={"error": "invalid_stt_response"},
        )
    except WhisperInvalidAudioError:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_audio"},
        )
    except WhisperAudioTooLargeError:
        return JSONResponse(
            status_code=413,
            content={"error": "audio_too_large"},
        )
    except InvalidWhisperResponseError:
        return JSONResponse(
            status_code=502,
            content={"error": "invalid_stt_response"},
        )
    except WhisperTimeoutError:
        return JSONResponse(
            status_code=504,
            content={"error": "stt_timeout"},
        )
    except WhisperTranscriptionFailedError:
        return JSONResponse(
            status_code=502,
            content={"error": "stt_transcription_failed"},
        )
    except WhisperUnavailableError:
        return JSONResponse(
            status_code=503,
            content={"error": "stt_unavailable"},
        )

    return await _generate_draft(payload, clinical_client)
