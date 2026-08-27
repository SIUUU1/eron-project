from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.ed.stay import LatestVital


class VitalPoint(BaseModel):
    measured_at: datetime
    heart_rate: float | None = None
    resp_rate: float | None = None
    sbp: float | None = None
    dbp: float | None = None
    spo2: float | None = None
    temperature_c: float | None = None
    rhythm: str | None = Field(None, description="원본의 약 96%가 결측이다.")
    pain: str | None = None
    consciousness: str | None = Field(None, description="항상 null — ED 테이블에 없다.")


class VitalsMeta(BaseModel):
    outlier_filtered: bool = True
    temperature_unit: str = "celsius"
    is_demo_timeline: bool = True
    notice: str = (
        "결측이 많다: temperature 약 36%, spo2 약 9%, sbp/dbp 약 5%. "
        "차트에서 선 끊김 처리가 필요하다."
    )


class VitalsResponse(BaseModel):
    stay_id: str
    vitals: list[VitalPoint]
    latest: LatestVital
    count: int
    meta: VitalsMeta
