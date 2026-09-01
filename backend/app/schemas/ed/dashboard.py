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
    status: str = Field(
        ...,
        description=(
            "critical(재평가 필요) | moderate(관찰 필요) | low(저위험) | "
            "pending(환자는 있으나 첫 예측 전) | empty(빈 병상)"
        ),
    )
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
    pending: int = Field(
        0, description="환자는 있으나 아직 첫 예측이 없는 병상. 위험도 카운트에 넣지 않는다"
    )
    empty: int


class BedsMeta(BaseModel):
    is_demo_assignment: bool = True
    status_source: str = Field(
        "prediction",
        description=(
            "병상 색상의 근거. 항상 'prediction' 이다 — 예측이 없는 환자는 색을 "
            "대신 칠하지 않고 pending(흰색)으로 둔다. "
            "'none' 이면 아직 어떤 환자도 예측이 도래하지 않은 상태다."
        ),
    )


class BedsResponse(BaseModel):
    summary: BedSummary
    zones: list[BedZone]
    meta: BedsMeta


class AlertItem(BaseModel):
    """경보가 켜진 시점 1건. app.prediction 에서 파생한다(별도 적재 없음)."""

    id: int = Field(description="근거가 된 app.prediction 행 id — 알림의 고유 식별자")
    stay_id: str
    display_name: str | None = None
    alert_time: datetime = Field(description="경보가 켜진 예측 시점 (데모 시간축)")
    level: str
    band: str | None = Field(
        default=None,
        description="모델 3구간 — green(저위험) | amber(관찰 필요) | red(재평가 필요)",
    )
    risk_probability: float | None = Field(
        default=None, description="그 시점의 보정 확률 (0~1)"
    )
    message: str = Field(
        description=(
            "모델이 만든 기여 신호 문장. **악화의 원인이 아니다.** "
            "설명이 없으면 경보 사실만 알리는 문구가 들어간다."
        )
    )
    reason_type: str | None = Field(
        default=None, description="risk_increase_signal | current_risk_signal"
    )
    acknowledged_at: datetime | None = Field(
        default=None,
        description=(
            "의료진 재검토 완료 시각. **이 알림의 최신 예측 시점에 대한** 확인만 유효하며, "
            "다음 예측이 생기면 자동으로 null 로 돌아간다."
        ),
    )


class AlertsResponse(BaseModel):
    items: list[AlertItem]
    unread_count: int = Field(
        default=0,
        description=(
            "아직 확인하지 않은 재검토 필요(red) 알림 **건수**. 종 아이콘 숫자다. "
            "같은 환자라도 예측 시점이 다르면 별도로 센다. "
            "저장된 숫자가 아니라 예측·확인기록에서 매번 계산한다."
        ),
    )
    meta: Meta


class AlertAckResult(BaseModel):
    """의료진 재검토 처리 결과."""

    ed_stay_id: str
    acknowledged: int = Field(description="이번에 확인 처리한 알림 수(이미 확인된 건 제외)")
    unread_count: int = Field(description="처리 후 전체에 남은 미확인 알림 수")


class ReassessItem(BaseModel):
    stay_id: str
    display_name: str | None = None
    risk_level: str | None = None
    risk_band: str | None = Field(
        None, description="모델 3구간 — green(저위험) | amber(관찰 필요) | red(재평가 필요)"
    )
    risk_probability: float | None = None
    bed_id: str | None = Field(None, description="배정 병상. 데모 배정이며 없으면 null")
    acuity: int | None = None
    due_minutes: int
    due_label: str


class ReassessResponse(BaseModel):
    items: list[ReassessItem]
    meta: BedsMeta
