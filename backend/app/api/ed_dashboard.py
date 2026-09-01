"""응급실 현황 대시보드 API (읽기 전용)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.repositories import dashboard as repo
from app.schemas.ed.dashboard import (
    AlertAckResult,
    AlertsResponse,
    BedsResponse,
    DashboardSummary,
    ReassessResponse,
)
from app.services import ed as svc

router = APIRouter(prefix="/api/ed", tags=["ED Dashboard"])

RISK_BANDS = {"green", "amber", "red"}


@router.get(
    "/dashboard/summary",
    response_model=DashboardSummary,
    summary="현황 요약",
    description=(
        "재실 환자 수와 위험도 분포. 모델 미연동 시 위험도 카운트는 0 이고 "
        "unassessed 에 전체가 잡힌다."
    ),
)
def get_summary(db: Session = Depends(get_db)) -> DashboardSummary:
    row = repo.summary_counts(db)
    return DashboardSummary(
        total=row["total"],
        discharged=row["discharged"],
        critical=row["critical"],
        rising=row["rising"],
        watch=row["watch"],
        stable=row["stable"],
        unassessed=row["unassessed"],
        ai_alerts_today=repo.alerts_today(db),
        meta=svc.build_meta(
            repo.cohort_size(db), model_connected=repo.has_predictions(db)
        ),
    )


@router.get(
    "/dashboard/beds",
    response_model=BedsResponse,
    summary="병상 현황판",
    description=(
        "병상 배치와 장비 표기는 MIMIC 에 없는 데모 값이다 (meta.is_demo_assignment). "
        "환자명·나이·성별·위험도는 실데이터다. 예측이 없으면 색상은 ESI 중증도로 대체된다."
    ),
)
def get_beds(db: Session = Depends(get_db)) -> BedsResponse:
    rows = repo.list_beds(db)
    zones, summary, any_prediction = svc.build_bed_zones(rows)
    return BedsResponse(summary=summary, zones=zones, meta=svc.beds_meta(any_prediction))


@router.get(
    "/alerts",
    response_model=AlertsResponse,
    summary="실시간 AI 경고",
    description=(
        "app.prediction 에서 파생한다. **현재 최신 예측이 재검토 필요인 환자만** 나오며, "
        "`band` 로 구간을, `latest_only` 로 환자당 1건 여부를 정한다. "
        "퇴실 환자와 미도래 예측은 제외한다. "
        "경보가 **꺼져 있다가 켜진 시점**만 1건으로 세므로 "
        "같은 환자가 매시간 반복되지 않는다. message 는 모델이 만든 기여 신호 문장이며 "
        "악화의 원인이 아니다. 예측이 없으면 빈 배열 — 가짜 경고를 만들지 않는다."
    ),
)
def get_alerts(
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    since: str | None = Query(None, description="ISO 8601"),
    band: str | None = Query(
        None, description="green(저위험) | amber(관찰 필요) | red(재평가 필요)"
    ),
    latest_only: bool = Query(
        False,
        description=(
            "true 면 환자당 최신 알림 1건만 (응급실 현황의 실시간 AI 경고). "
            "false 면 그 환자의 재검토 필요 시점을 모두 (종 알림 목록)."
        ),
    ),
) -> AlertsResponse:
    if band is not None and band not in RISK_BANDS:
        raise HTTPException(status_code=400, detail=f"band must be one of {sorted(RISK_BANDS)}")
    rows = repo.list_alerts(
        db, limit=limit, since=since, band=band, latest_only=latest_only
    )
    return AlertsResponse(
        items=[svc.to_alert_item(r) for r in rows],
        # 종 아이콘 숫자. 목록의 band/limit 과 무관하게 항상 같은 정의로 센다.
        unread_count=repo.unread_alert_count(db),
        meta=svc.build_meta(
            repo.cohort_size(db), model_connected=repo.has_predictions(db)
        ),
    )


@router.post(
    "/alerts/{stay_id}/acknowledge",
    response_model=AlertAckResult,
    summary="의료진 재검토",
    description=(
        "그 환자의 **미확인 재검토 필요 알림을 한 번에** 확인 처리한다. "
        "대상은 현재 데모 시각까지 도래한 알림뿐이며, 다른 환자는 건드리지 않는다. "
        "여러 번 눌러도 결과가 같고(멱등), 모델의 alarm/band 는 바뀌지 않는다. "
        "다음 예측에서 다시 red 가 나오면 새 알림이므로 버튼이 다시 활성화된다."
    ),
)
def acknowledge_alert(
    stay_id: int,
    db: Session = Depends(get_db),
    by: str | None = Query(None, description="확인한 의료진 표기(선택)"),
) -> AlertAckResult:
    acknowledged = repo.acknowledge_stay(db, stay_id, by=by)
    return AlertAckResult(
        ed_stay_id=str(stay_id),
        acknowledged=acknowledged,
        unread_count=repo.unread_alert_count(db),
    )


@router.get(
    "/reassess-queue",
    response_model=ReassessResponse,
    summary="위험 환자 재평가 우선순위",
    description=(
        "예측이 있으면 확률 순, 없으면 ESI 중증도 순으로 정렬한다. "
        "meta.status_source 로 어느 쪽인지 알 수 있다."
    ),
)
def get_reassess_queue(db: Session = Depends(get_db)) -> ReassessResponse:
    rows = repo.reassess_candidates(db)
    items, any_prediction = svc.to_reassess_items(rows)
    return ReassessResponse(items=items, meta=svc.reassess_meta(any_prediction))
