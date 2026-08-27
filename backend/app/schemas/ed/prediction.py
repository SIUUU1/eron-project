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


class LatestPrediction(BaseModel):
    risk_probability: float | None = None
    risk_level: str | None = None
    risk_factors: list[str] = Field(
        default_factory=list,
        description="TODO — 모델 output 구조 미확인. 확정 전까지 빈 배열.",
    )
    recommendations: list[str] = Field(
        default_factory=list,
        description="TODO — 모델 output 구조 미확인. 확정 전까지 빈 배열.",
    )


class PredictionMeta(BaseModel):
    model_connected: bool = False
    notice: str = (
        "모델 미연동. 저장소에 inference 코드가 없어 예측을 생성하지 않는다. "
        "predictions 가 빈 배열이면 프론트는 'AI 분석 대기 중' 빈 상태를 표시해야 한다."
    )


class PredictionsResponse(BaseModel):
    stay_id: str
    predictions: list[PredictionPoint]
    latest: LatestPrediction
    count: int
    meta: PredictionMeta
