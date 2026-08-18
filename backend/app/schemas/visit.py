from datetime import datetime

from pydantic import BaseModel, ConfigDict


class VisitCreate(BaseModel):
    patient_id: int
    arrival_time: datetime
    triage_level: int | None = None
    chief_complaint: str | None = None
    status: str = "active"


class VisitUpdate(BaseModel):
    triage_level: int | None = None
    chief_complaint: str | None = None
    status: str | None = None


class VisitResponse(BaseModel):
    id: int
    patient_id: int
    arrival_time: datetime
    triage_level: int | None
    chief_complaint: str | None
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)