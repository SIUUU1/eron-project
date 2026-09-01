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


# 병상 상태 = 화면 색. 모델 3구간을 그대로 쓴다.
# 🔑 risk_level(4단계, .env RISK_*)로 매핑하면 화면이 어긋난다 — 13.3~40% 환자가
#    환자 목록에서는 "재평가 필요"(red)인데 현황판에서는 "관찰 필요"로 칠해졌다.
#
#   empty    빈 병상            회색
#   pending  환자는 있지만 아직 첫 예측 전   흰색  ← 위험도 카운트에 넣지 않는다
#   critical 재평가 필요 (red)
#   moderate 관찰 필요   (amber)
#   low      저위험      (green)
_BED_STATUS_BY_BAND = {"red": "critical", "amber": "moderate", "green": "low"}

BED_STATUS_PENDING = "pending"


def bed_status(band: str | None) -> str:
    """병상 상태. **예측이 없으면 pending 이다.**

    🔑 예전에는 예측이 없을 때 triage acuity(ESI)로 색을 대신 칠했다. 그러면 ED 도착
       +1시간이 안 된 환자가 이미 위험도가 판정된 것처럼 보이고, 상태별 환자 수도
       실제 예측과 어긋난다. 판정할 근거가 없으면 없다고 표시한다.

    ⚠ band 가 None 인 경우는 두 가지다 — 첫 예측 시점(ED 도착 +1h) 이전이거나,
      데모 시계를 되돌려 그 예측이 아직 도래하지 않은 경우. 둘 다 pending 이 맞다.
    """
    if band is None:
        return BED_STATUS_PENDING
    return _BED_STATUS_BY_BAND.get(band, BED_STATUS_PENDING)


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
