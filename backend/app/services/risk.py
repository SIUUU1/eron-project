"""위험도 등급 산출.

모델이 아직 연동되지 않아 예측이 비어 있을 수 있다. 그 경우 확률을 지어내지
않고 None 을 반환한다 (프론트는 "AI 분석 대기 중" 빈 상태를 표시한다).

병상 현황판처럼 반드시 4단계 색이 필요한 곳에서는, 예측이 없을 때
triage acuity(ESI) 를 명시적 대체 근거로 쓴다. 응답 meta 에 출처를 밝힌다.
"""

from __future__ import annotations

from app.core.config import settings

RiskLevel = str  # "stable" | "watch" | "rising" | "critical"


def level_from_probability(probability: float | None) -> RiskLevel | None:
    if probability is None:
        return None
    if probability >= settings.risk_critical:
        return "critical"
    if probability >= settings.risk_rising:
        return "rising"
    if probability >= settings.risk_watch:
        return "watch"
    return "stable"


def level_from_acuity(acuity: int | None) -> RiskLevel | None:
    """예측이 없을 때 쓰는 대체 등급. 예측 확률이 아니라 중증도 분류다."""
    if acuity is None:
        return None
    if acuity <= 1:
        return "critical"
    if acuity == 2:
        return "rising"
    if acuity == 3:
        return "watch"
    return "stable"


# 병상 현황판은 4단계(critical/moderate/low/empty)만 쓴다.
_BED_STATUS = {"critical": "critical", "rising": "moderate", "watch": "moderate", "stable": "low"}


def bed_status(level: RiskLevel | None) -> str:
    if level is None:
        return "low"
    return _BED_STATUS.get(level, "low")


# 재평가 우선순위. frontend settings.tsx 의 임계값 UI 와 정합.
_DUE = {
    "critical": (0, "즉시"),
    "rising": (10, "10분 내"),
    "watch": (30, "30분 내"),
}


def due_for(level: RiskLevel | None) -> tuple[int, str] | None:
    if level is None:
        return None
    return _DUE.get(level)
