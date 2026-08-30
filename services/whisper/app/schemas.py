from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class LinkSet(BaseModel):
    self: str
    result: str


class JobStatusResponse(BaseModel):
    api_version: Literal["v1"] = "v1"
    id: str
    request_id: str
    status: Literal["queued", "processing", "completed", "failed"]
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    source: dict
    error_code: str | None = None
    links: LinkSet


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    api_version: Literal["v1"] = "v1"
    model: dict
    queue: dict
    limits: dict


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str
    details: dict | list | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


COMMON_ERROR_RESPONSES = {
    status: {"model": ErrorResponse}
    for status in (400, 401, 404, 409, 413, 415, 422, 429, 500)
}
