from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.ed.common import Meta


class DashboardSummary(BaseModel):
    total: int = Field(..., description="현재 재실 중인 환자 수 (퇴실자 제외)")
    discharged: int = Field(0, description="코호트 중 이미 퇴실한 환자 수")
    critical: int
    rising: int
    watch: int
    stable: int
    unassessed: int = Field(0, description="예측이 없어 위험도를 산출할 수 없는 환자 수")
    ai_alerts_today: int = 0
    meta: Meta


class BedItem(BaseModel):
    bed_id: str
    status: str = Field(..., description="critical | moderate | low | empty")
    stay_id: str | None = None
    display_name: str | None = None
    age: int | None = None
    sex: str | None = None
    devices: list[str] = Field(
        default_factory=list,
        description="E=ECMO, V=인공호흡기, C=CRRT. MIMIC 에 없는 정보라 현재는 항상 빈 배열.",
    )


class BedZone(BaseModel):
    zone: str
    beds: list[BedItem]


class BedSummary(BaseModel):
    total: int
    critical: int
    moderate: int
    low: int
    empty: int


class BedsMeta(BaseModel):
    is_demo_assignment: bool = True
    status_source: str = Field(
        "triage_acuity",
        description=(
            "병상 색상의 근거. 'prediction' 이면 모델 예측, "
            "'triage_acuity' 면 예측이 없어 ESI 중증도로 대체한 것이다."
        ),
    )


class BedsResponse(BaseModel):
    summary: BedSummary
    zones: list[BedZone]
    meta: BedsMeta


class AlertItem(BaseModel):
    id: int
    stay_id: str
    display_name: str | None = None
    alert_time: datetime
    level: str
    message: str
    acknowledged_at: datetime | None = None


class AlertsResponse(BaseModel):
    items: list[AlertItem]
    meta: Meta


class ReassessItem(BaseModel):
    stay_id: str
    display_name: str | None = None
    risk_level: str | None = None
    risk_probability: float | None = None
    acuity: int | None = None
    due_minutes: int
    due_label: str


class ReassessResponse(BaseModel):
    items: list[ReassessItem]
    meta: BedsMeta
