"""응급실 현황 대시보드 API (읽기 전용)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.repositories import dashboard as repo
from app.schemas.ed.dashboard import (
    AlertsResponse,
    BedsResponse,
    DashboardSummary,
    ReassessResponse,
)
from app.services import ed as svc

router = APIRouter(prefix="/api/ed", tags=["ED Dashboard"])


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
    description="모델 미연동 시 빈 배열을 반환한다. 가짜 경고를 만들지 않는다.",
)
def get_alerts(
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    since: str | None = Query(None, description="ISO 8601"),
) -> AlertsResponse:
    rows = repo.list_alerts(db, limit=limit, since=since)
    return AlertsResponse(
        items=[svc.to_alert_item(r) for r in rows],
        meta=svc.build_meta(
            repo.cohort_size(db), model_connected=repo.has_predictions(db)
        ),
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
    return ReassessResponse(items=items, meta=svc.beds_meta(any_prediction))
