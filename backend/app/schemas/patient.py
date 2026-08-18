from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class PatientCreate(BaseModel):
    patient_number: str
    name: str
    birth_date: date | None = None
    gender: str | None = None


class PatientUpdate(BaseModel):
    name: str | None = None
    birth_date: date | None = None
    gender: str | None = None


class PatientResponse(BaseModel):
    id: int
    patient_number: str
    name: str
    birth_date: date | None
    gender: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)