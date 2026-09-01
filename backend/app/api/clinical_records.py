"""Public endpoint for generating a reviewable emergency-record draft."""

from __future__ import annotations

from collections.abc import AsyncIterator
import json

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
import httpx2
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import SessionLocal
from app.models.clinical_record import ClinicalRecord
from app.schemas.clinical_record import (
    ClinicalRecordPersistedResponse,
    ClinicalRecordSaveRequest,
    ClinicalRecordSignRequest,
    WhisperDraftRequest,
)
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


def migrate_legacy_allergy_key(value):
    """Recursively migrate persisted JSON from drug_allergy to allergy."""

    if isinstance(value, list):
        return [migrate_legacy_allergy_key(item) for item in value]
    if not isinstance(value, dict):
        return value
    migrated = {
        key: migrate_legacy_allergy_key(item)
        for key, item in value.items()
        if key != "drug_allergy"
    }
    if "allergy" not in migrated and "drug_allergy" in value:
        migrated["allergy"] = migrate_legacy_allergy_key(value["drug_allergy"])
    return migrated


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_ed_stay(db: Session, ed_stay_id: str) -> None:
    exists = db.execute(
        text("SELECT 1 FROM mimic.edstays WHERE stay_id = :stay_id"),
        {"stay_id": ed_stay_id},
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(status_code=404, detail="ED stay not found")


@router.get(
    "/by-stay/{ed_stay_id}",
    response_model=ClinicalRecordPersistedResponse | None,
)
def get_persisted_clinical_record(
    ed_stay_id: str,
    db: Session = Depends(get_db),
):
    record = db.scalar(select(ClinicalRecord).where(ClinicalRecord.ed_stay_id == ed_stay_id))
    if record is not None:
        migrated_payload = migrate_legacy_allergy_key(record.record_payload)
        if migrated_payload != record.record_payload:
            record.record_payload = migrated_payload
            db.commit()
            db.refresh(record)
    return record


@router.put(
    "/by-stay/{ed_stay_id}",
    response_model=ClinicalRecordPersistedResponse,
)
def save_clinical_record_draft(
    ed_stay_id: str,
    payload: ClinicalRecordSaveRequest,
    db: Session = Depends(get_db),
):
    require_ed_stay(db, ed_stay_id)
    record_payload = migrate_legacy_allergy_key(payload.record_payload)
    selected_kcd = (
        [item.model_dump() for item in payload.selected_kcd]
        if isinstance(payload.selected_kcd, list)
        else payload.selected_kcd.model_dump()
        if payload.selected_kcd
        else None
    )
    record = db.scalar(
        select(ClinicalRecord)
        .where(ClinicalRecord.ed_stay_id == ed_stay_id)
        .with_for_update()
    )
    if record and record.status == "SIGNED":
        raise HTTPException(status_code=409, detail="SIGNED record is immutable")
    if record is None:
        record = ClinicalRecord(
            ed_stay_id=ed_stay_id,
            status="DRAFT",
            record_payload=record_payload,
            selected_kcd=selected_kcd,
            clinician_id=payload.clinician_id,
            clinician_name=payload.clinician_name,
        )
        db.add(record)
    else:
        record.record_payload = record_payload
        record.selected_kcd = selected_kcd
        record.clinician_id = payload.clinician_id
        record.clinician_name = payload.clinician_name
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="Clinical record already exists") from error
    db.refresh(record)
    return record


@router.post(
    "/{record_id}/sign",
    response_model=ClinicalRecordPersistedResponse,
)
def sign_clinical_record(
    record_id: int,
    payload: ClinicalRecordSignRequest,
    db: Session = Depends(get_db),
):
    record = db.scalar(
        select(ClinicalRecord).where(ClinicalRecord.id == record_id).with_for_update()
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Clinical record not found")
    if record.status != "DRAFT":
        raise HTTPException(status_code=409, detail="Clinical record is already SIGNED")
    record.status = "SIGNED"
    record.clinician_id = payload.clinician_id
    record.clinician_name = payload.clinician_name
    record.signed_by = payload.clinician_id
    record.signed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(record)
    return record


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
