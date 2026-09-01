from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class PredictionPoint(BaseModel):
    prediction_time: datetime
    t_idx: int | None = None
    horizon_minutes: int | None = None
    risk_probability: float
    risk_level: str
    model_version: str


class RiskSignal(BaseModel):
    """설명 한 줄. riskmodel 이 만든 문장을 그대로 옮긴다.

    v3 부터 **model feature 1개** 단위다(vital/lab 그룹 합산이 아니다).
    """

    feature: str = Field(description="모델 feature 명 (heart_rate_last, lab_lactate_dt 등)")
    feature_label: str = Field(description="사람이 읽는 feature 이름")
    text: str = Field(description="화면에 그대로 쓰는 문장")
    value: float | None = Field(
        default=None, description="현재 값 (current_risk_signal)"
    )
    contribution: float | None = Field(
        default=None, description="현재 위험도에 대한 기여도 (current_risk_signal)"
    )
    previous_value: float | None = Field(
        default=None, description="직전 시점 값 (risk_increase_signal)"
    )
    current_value: float | None = Field(
        default=None, description="현재 시점 값 (risk_increase_signal)"
    )
    previous_contribution: float | None = None
    current_contribution: float | None = None
    delta_contribution: float | None = Field(
        default=None, description="직전 대비 기여도 증가분 (risk_increase_signal)"
    )
    contribution_space: str | None = Field(
        default=None,
        description=(
            "기여도의 단위 공간. lightgbm_raw_score_shap 이며 "
            "**보정 확률의 %p 가 아니다.**"
        ),
    )
    clinical_direction: str | None = Field(
        default=None, description="임상 방향 gate 판정. 노출된 악화 신호는 항상 worsening"
    )
    clinical_rule: str | None = Field(
        default=None, description="판정 근거 규칙 이름 (예: hr_severity_gate)"
    )
    clinical_gate_passed: bool | None = None
    previous_clinical_severity: float | None = None
    current_clinical_severity: float | None = None


class LatestPrediction(BaseModel):
    risk_probability: float | None = None
    risk_level: str | None = None
    risk_factors: list[str] = Field(
        default_factory=list,
        description=(
            "모델 예측에 기여한 주요 신호 문장. **악화의 원인이 아니다.** "
            "화면에 표시할 때 reason_notice 를 함께 보여야 한다."
        ),
    )
    risk_signals: list[RiskSignal] = Field(
        default_factory=list,
        description="risk_factors 와 같은 신호를 기여도까지 포함해 돌려준다.",
    )
    reason_type: str | None = Field(
        default=None,
        description=(
            "risk_increase_clinical_worsening_signal = 직전 대비 임상적 악화로 확인된 신호, "
            "risk_increase_without_confirmed_clinical_worsening_signal = 위험도는 올랐지만 "
            "임상 방향 gate 를 통과한 악화 변화가 없음(**risk_factors 가 비는 것이 정상**), "
            "current_risk_signal = 현재 위험도에 기여한 신호"
        ),
    )
    clinical_worsening_confirmed: bool | None = Field(
        default=None,
        description="위험 상승 시점에서 임상적 악화로 확인된 변화가 있었는가",
    )
    reason_title: str | None = Field(
        default=None,
        description="화면 제목. 모델이 만든 문구를 그대로 쓴다(직전 예측 대비 상승 기여 등)",
    )
    reason_basis: str | None = Field(
        default=None,
        description=(
            "exact_feature_delta_contribution | exact_feature_current_contribution"
        ),
    )
    reason_notice: str | None = Field(
        default=None, description="설명과 함께 반드시 표시해야 하는 문구"
    )
    risk_delta: float | None = Field(
        default=None, description="직전 예측 시점 대비 확률 변화. 첫 시점이면 null"
    )
    recommendations: list[str] = Field(
        default_factory=list,
        description="항상 빈 배열 — 악화 예측 모델은 권고를 생성하지 않는다.",
    )


NOTICE_DISCONNECTED = (
    "모델 미연동. 예측이 없어 위험도를 표시할 수 없다. "
    "predictions 가 빈 배열이면 프론트는 'AI 분석 대기 중' 빈 상태를 표시해야 한다."
)
NOTICE_CONNECTED = (
    "AI 예측은 의료진 의사결정 지원 정보이며 확정 진단이 아니다. "
    "risk_factors 는 모델 예측에 기여한 주요 신호이고 임상적 인과관계를 의미하지 않는다."
)


class PredictionMeta(BaseModel):
    model_connected: bool = False
    notice: str = NOTICE_DISCONNECTED

    @classmethod
    def for_rows(cls, has_rows: bool) -> "PredictionMeta":
        return cls(
            model_connected=has_rows,
            notice=NOTICE_CONNECTED if has_rows else NOTICE_DISCONNECTED,
        )


class PredictionsResponse(BaseModel):
    stay_id: str
    predictions: list[PredictionPoint]
    latest: LatestPrediction
    count: int
    meta: PredictionMeta


class PredictionRunResult(BaseModel):
    """예측 갱신 실행 요약."""

    stays: int = Field(description="코호트 stay 수 (app.cohort)")
    selected: int = Field(
        default=0,
        description="이번 실행에서 계산 대상으로 고른 stay 수 (due + 슬롯 조건)",
    )
    slot: str | None = Field(
        default=None,
        description="실행 기준 15분 슬롯(데모 축). all=true 면 null",
    )
    scored: int = Field(description="예측을 기록한 stay 수")
    rows: int = Field(description="기록한 예측 시점 수 (upsert 기준)")
    out_of_scope: int = Field(
        description="적용 범위 밖이라 예측하지 않은 stay 수 "
        "(18세 미만 · ED 도착 +1h 이전 · ED 퇴실 +2h 초과)"
    )
    failed: int = Field(description="예측 서비스 호출에 실패한 stay 수")
