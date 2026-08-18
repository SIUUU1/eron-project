from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RecordCreate(BaseModel):
    record_type: str
    content: str
    generated_by: str | None = None


class RecordUpdate(BaseModel):
    content: str | None = None
    confirmed: bool | None = None


class RecordResponse(BaseModel):
    id: int
    visit_id: int
    record_type: str
    content: str
    generated_by: str | None
    created_at: datetime
    confirmed_at: datetime | None

    model_config = ConfigDict(from_attributes=True)