"""HTTP adapter for the internal ClinicalNLP draft service."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
from jsonschema import Draft202012Validator


class InvalidClinicalNlpResponseError(Exception):
    pass


class ClinicalNlpUnavailableError(Exception):
    pass


class InvalidWhisperPayloadError(Exception):
    pass


class ClinicalNlpTimeoutError(Exception):
    pass


_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "clinical-workflow-v2.schema.json"
)
_WORKFLOW_VALIDATOR = Draft202012Validator(
    json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
)


class ClinicalNlpClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        http_client: httpx2.AsyncClient,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client

    async def create_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._http_client.post(
            f"{self._base_url}/v2/clinical-workflows",
            json=payload,
            timeout=self._timeout_seconds,
        )
        if response.status_code == 400:
            raise InvalidWhisperPayloadError
        if response.status_code == 504:
            raise ClinicalNlpTimeoutError
        if response.status_code != 200:
            raise ClinicalNlpUnavailableError
        try:
            result = response.json()
        except ValueError as exc:
            raise InvalidClinicalNlpResponseError from exc
        if not isinstance(result, dict) or next(
            _WORKFLOW_VALIDATOR.iter_errors(result),
            None,
        ):
            raise InvalidClinicalNlpResponseError
        if (
            result.get("record_status") != "DRAFT"
            or result.get("workflow_phase") != "DRAFT_GENERATION"
            or result.get("completed_at") is not None
        ):
            raise InvalidClinicalNlpResponseError
        return result
