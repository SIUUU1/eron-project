"""Public endpoint for generating a reviewable emergency-record draft."""

from __future__ import annotations

from collections.abc import AsyncIterator
import json

from fastapi import APIRouter, Depends, Request
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


router = APIRouter(prefix="/api/clinical-records", tags=["clinical-records"])


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
    except ClinicalNlpTimeoutError:
        return JSONResponse(
            status_code=504,
            content={"error": "clinicalnlp_timeout"},
        )
    except httpx2.TimeoutException:
        return JSONResponse(
            status_code=504,
            content={"error": "clinicalnlp_timeout"},
        )
    except ClinicalNlpUnavailableError:
        return JSONResponse(
            status_code=503,
            content={"error": "clinicalnlp_unavailable"},
        )
    except httpx2.RequestError:
        return JSONResponse(
            status_code=503,
            content={"error": "clinicalnlp_unavailable"},
        )
