from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .audio import probe_audio_duration, safe_audio_suffix
from .config import Settings
from .errors import APIError, ERROR_MESSAGES, api_error
from .groq_transcriber import GroqWhisperTranscriber
from .job_store import JobStore
from .jobs import JobManager, Transcriber
from .schemas import COMMON_ERROR_RESPONSES, HealthResponse, JobStatusResponse


logger = logging.getLogger("whisper.api")
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
JOB_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
UPLOAD_CHUNK_BYTES = 1024 * 1024


def _new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", _new_request_id())


def _error_payload(
    request: Request,
    *,
    code: str,
    message: str,
    details: Any = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": _request_id(request),
            "details": details,
        }
    }


def _job_response(job: dict[str, Any]) -> dict[str, Any]:
    job_id = job["id"]
    return {
        "api_version": "v1",
        "id": job_id,
        "request_id": job["request_id"],
        "status": job["status"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "completed_at": job["completed_at"],
        "source": {
            "content_type": job["content_type"],
            "size_bytes": job["size_bytes"],
            "duration_seconds": round(float(job["duration_seconds"]), 3),
        },
        "error_code": job["error_code"],
        "links": {
            "self": f"/v1/transcriptions/{job_id}",
            "result": f"/v1/transcriptions/{job_id}/result",
        },
    }


def create_api_app(
    *,
    settings: Settings | None = None,
    transcriber: Transcriber | None = None,
) -> FastAPI:
    configured_settings = settings or Settings.from_env()
    configured_transcriber = transcriber or GroqWhisperTranscriber()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configured_settings.ensure_directories()
        store = JobStore(configured_settings.database_path)
        manager = JobManager(configured_settings, store, configured_transcriber)
        application.state.settings = configured_settings
        application.state.transcriber = configured_transcriber
        application.state.store = store
        application.state.manager = manager
        await manager.start()
        try:
            yield
        finally:
            await manager.stop()

    application = FastAPI(
        title="ER:ON Whisper Transcription API",
        description="Internal asynchronous Groq Whisper transcription service.",
        version="1.0.0",
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        incoming = request.headers.get("X-Request-ID", "")
        request.state.request_id = (
            incoming if REQUEST_ID_PATTERN.fullmatch(incoming) else _new_request_id()
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(
                request,
                code=exc.code,
                message=exc.message,
                details=exc.details,
            ),
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        safe_details = [
            {"location": list(error["loc"]), "type": error["type"]}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_error_payload(
                request,
                code="VALIDATION_ERROR",
                message=ERROR_MESSAGES["VALIDATION_ERROR"],
                details=safe_details,
            ),
        )

    @application.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = "JOB_NOT_FOUND" if exc.status_code == 404 else "VALIDATION_ERROR"
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(
                request,
                code=code,
                message=ERROR_MESSAGES[code],
            ),
        )

    @application.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_api_error request_id=%s error_type=%s",
            _request_id(request),
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                request,
                code="INTERNAL_ERROR",
                message=ERROR_MESSAGES["INTERNAL_ERROR"],
            ),
        )

    @application.get(
        "/v1/health",
        response_model=HealthResponse,
        responses=COMMON_ERROR_RESPONSES,
    )
    async def health(request: Request) -> dict[str, Any]:
        manager: JobManager = request.app.state.manager
        active_transcriber: Transcriber = request.app.state.transcriber
        limits: Settings = request.app.state.settings
        healthy = manager.is_running and active_transcriber.is_loaded
        return {
            "status": "ok" if healthy else "degraded",
            "api_version": "v1",
            "model": active_transcriber.status(),
            "queue": {
                "mode": "concurrent" if limits.worker_count > 1 else "sequential",
                "pending": manager.queue.qsize(),
                "capacity": limits.queue_capacity,
                "worker_running": manager.is_running,
                "workers": limits.worker_count,
            },
            "limits": {
                "max_upload_mb": limits.max_upload_mb,
                "max_audio_duration_seconds": limits.max_audio_duration_seconds,
            },
        }

    @application.post(
        "/v1/transcriptions",
        status_code=202,
        response_model=JobStatusResponse,
        responses=COMMON_ERROR_RESPONSES,
    )
    async def create_transcription(
        request: Request,
        audio: Annotated[UploadFile, File(description="Audio file to transcribe")],
    ) -> dict[str, Any]:
        limits: Settings = request.app.state.settings
        manager: JobManager = request.app.state.manager
        store: JobStore = request.app.state.store
        if manager.is_full:
            raise api_error("QUEUE_FULL", 429)
        suffix = safe_audio_suffix(audio.filename, audio.content_type)
        if suffix is None:
            await audio.close()
            raise api_error("UNSUPPORTED_MEDIA_TYPE", 415)
        job_id = uuid.uuid4().hex
        audio_path = limits.spool_dir / f"{job_id}{suffix}"
        size_bytes = 0
        accepted = False
        try:
            with audio_path.open("xb") as destination:
                while chunk := await audio.read(UPLOAD_CHUNK_BYTES):
                    size_bytes += len(chunk)
                    if size_bytes > limits.max_upload_bytes:
                        raise api_error(
                            "FILE_TOO_LARGE",
                            413,
                            details={"max_upload_mb": limits.max_upload_mb},
                        )
                    destination.write(chunk)
            if size_bytes == 0:
                raise api_error("EMPTY_AUDIO", 400)
            try:
                duration_seconds = await asyncio.to_thread(
                    probe_audio_duration, audio_path
                )
            except ValueError:
                raise api_error("INVALID_AUDIO", 400) from None
            if duration_seconds > limits.max_audio_duration_seconds:
                raise api_error(
                    "AUDIO_TOO_LONG",
                    413,
                    details={
                        "duration_seconds": round(duration_seconds, 3),
                        "max_audio_duration_seconds": limits.max_audio_duration_seconds,
                    },
                )
            job = store.create(
                job_id=job_id,
                request_id=_request_id(request),
                content_type=audio.content_type,
                size_bytes=size_bytes,
                duration_seconds=duration_seconds,
                audio_path=audio_path,
            )
            if not await manager.enqueue(job):
                store.update_status(job_id, "failed", error_code="QUEUE_FULL")
                raise api_error("QUEUE_FULL", 429)
            accepted = True
            return _job_response(job)
        finally:
            await audio.close()
            if not accepted:
                audio_path.unlink(missing_ok=True)

    @application.get(
        "/v1/transcriptions/{job_id}",
        response_model=JobStatusResponse,
        responses=COMMON_ERROR_RESPONSES,
    )
    async def get_transcription(request: Request, job_id: str) -> dict[str, Any]:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise api_error("JOB_NOT_FOUND", 404)
        job = request.app.state.store.get(job_id)
        if job is None:
            raise api_error("JOB_NOT_FOUND", 404)
        return _job_response(job)

    @application.get(
        "/v1/transcriptions/{job_id}/result",
        responses=COMMON_ERROR_RESPONSES,
    )
    async def get_transcription_result(request: Request, job_id: str) -> JSONResponse:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise api_error("JOB_NOT_FOUND", 404)
        job = request.app.state.store.get(job_id)
        if job is None:
            raise api_error("JOB_NOT_FOUND", 404)
        if job["status"] == "failed":
            raise api_error(
                "TRANSCRIPTION_FAILED",
                422,
                details={"job_id": job_id, "error_code": job["error_code"]},
            )
        if job["status"] != "completed":
            raise api_error(
                "RESULT_NOT_READY",
                409,
                details={"job_id": job_id, "status": job["status"]},
            )
        result_path = Path(job["result_path"])
        if not result_path.is_file():
            raise api_error("INTERNAL_ERROR", 500)
        return JSONResponse(content=json.loads(result_path.read_text(encoding="utf-8")))

    return application


app = create_api_app()
