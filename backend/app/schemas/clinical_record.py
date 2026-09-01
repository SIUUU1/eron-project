"""Request contract for ClinicalNLP draft generation."""

from __future__ import annotations

import math
from typing import Self

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr, model_validator


class WhisperSegment(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: StrictStr | StrictInt
    start: StrictFloat | StrictInt
    end: StrictFloat | StrictInt
    text: StrictStr

    @model_validator(mode="after")
    def validate_timing(self) -> Self:
        if not math.isfinite(self.start) or not math.isfinite(self.end):
            raise ValueError("segment timing must be finite")
        if self.start > self.end:
            raise ValueError("segment start must not be after end")
        return self


class WhisperDraftRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    segments: list[WhisperSegment]

    @model_validator(mode="after")
    def validate_unique_segment_ids(self) -> Self:
        segment_ids = [segment.id for segment in self.segments]
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("segment ids must be unique")
        return self


class SelectedKcd(BaseModel):
    code: str
    name: str
    is_rule_out: bool = False


class ClinicalRecordSaveRequest(BaseModel):
    record_payload: dict[str, Any]
    selected_kcd: list[SelectedKcd] | SelectedKcd | None = None
    clinician_id: str = Field(min_length=1, max_length=50)
    clinician_name: str = Field(min_length=1, max_length=100)


class ClinicalRecordSignRequest(BaseModel):
    clinician_id: str = Field(min_length=1, max_length=50)
    clinician_name: str = Field(min_length=1, max_length=100)


class ClinicalRecordPersistedResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ed_stay_id: str
    status: Literal["DRAFT", "SIGNED"]
    record_payload: dict[str, Any]
    selected_kcd: list[SelectedKcd] | SelectedKcd | None
    clinician_id: str
    clinician_name: str
    created_at: datetime
    updated_at: datetime
    signed_by: str | None
    signed_at: datetime | None
