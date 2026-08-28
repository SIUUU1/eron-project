"""Request contract for ClinicalNLP draft generation."""

from __future__ import annotations

import math
from typing import Self

from pydantic import BaseModel, ConfigDict, StrictFloat, StrictInt, StrictStr, model_validator


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
