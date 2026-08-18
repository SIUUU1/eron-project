from datetime import datetime

from pydantic import BaseModel, ConfigDict


class VitalCreate(BaseModel):
    measured_at: datetime

    heart_rate: float | None = None
    respiratory_rate: float | None = None

    systolic_bp: float | None = None
    diastolic_bp: float | None = None

    temperature: float | None = None
    spo2: float | None = None

    consciousness: str | None = None


class VitalUpdate(BaseModel):
    measured_at: datetime | None = None

    heart_rate: float | None = None
    respiratory_rate: float | None = None

    systolic_bp: float | None = None
    diastolic_bp: float | None = None

    temperature: float | None = None
    spo2: float | None = None

    consciousness: str | None = None


class VitalResponse(BaseModel):
    id: int
    visit_id: int
    measured_at: datetime

    heart_rate: float | None
    respiratory_rate: float | None

    systolic_bp: float | None
    diastolic_bp: float | None

    temperature: float | None
    spo2: float | None

    consciousness: str | None

    model_config = ConfigDict(from_attributes=True)