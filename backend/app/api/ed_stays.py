"""ED stay 조회 API (읽기 전용).

기존 /api/patients 는 자체 CRUD 도메인이라 그대로 두고, MIMIC 기반 조회는
/api/ed/* 신규 네임스페이스를 쓴다 (docs/architecture.md §6, D4 확정).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.repositories import dashboard as dashboard_repo
from app.repositories import ed_stays as repo
from app.schemas.ed.prediction import PredictionMeta, PredictionsResponse
from app.schemas.ed.stay import EdStayDetail, EdStayPage, LatestVital
from app.schemas.ed.vitals import VitalsMeta, VitalsResponse
from app.services import ed as svc

router = APIRouter(prefix="/api/ed", tags=["ED Stays"])

RISK_LEVELS = {"critical", "rising", "watch", "stable"}


@router.get(
    "/stays",
    response_model=EdStayPage,
    summary="응급실 환자 목록",
    description=(
        "코호트에 적재된 ED stay 를 페이지 단위로 반환한다. "
        "stay 당 최신 vital·예측 1건만 LATERAL 로 읽으므로 N+1 이 발생하지 않는다."
    ),
    responses={400: {"description": "잘못된 파라미터"}},
)
def get_stays(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    risk_level: str | None = Query(None, description="critical | rising | watch | stable"),
    acuity: int | None = Query(None, ge=1, le=5, description="ESI 1~5"),
    search: str | None = Query(None, description="stay_id 접두 일치 또는 주증상 부분 일치"),
    sort: str = Query("risk", description="risk | arrival | acuity_mix"),
) -> EdStayPage:
    if risk_level is not None and risk_level not in RISK_LEVELS:
        raise HTTPException(status_code=400, detail=f"risk_level must be one of {sorted(RISK_LEVELS)}")
    if sort not in repo.SORT_KEYS:
        raise HTTPException(
            status_code=400, detail=f"sort must be one of {sorted(repo.SORT_KEYS)}"
        )

    total = repo.count_stays(db, risk_level=risk_level, acuity=acuity, search=search)
    rows = repo.list_stays(
        db, page=page, page_size=page_size,
        risk_level=risk_level, acuity=acuity, search=search, sort=sort,
    )
    return EdStayPage(
        items=[svc.to_list_item(r) for r in rows],
        page=page,
        page_size=page_size,
        total=total,
        meta=svc.build_meta(
            dashboard_repo.cohort_size(db),
            model_connected=dashboard_repo.has_predictions(db),
        ),
    )


@router.get(
    "/stays/{stay_id}",
    response_model=EdStayDetail,
    summary="환자 상세",
    responses={404: {"description": "코호트에 없는 stay_id"}},
)
def get_stay(
    stay_id: int = Path(..., description="MIMIC-IV-ED stay_id"),
    db: Session = Depends(get_db),
) -> EdStayDetail:
    row = repo.get_stay(db, stay_id)
    if row is None:
        raise HTTPException(status_code=404, detail="ED stay not found")
    return svc.to_detail(
        row,
        dashboard_repo.cohort_size(db),
        model_connected=dashboard_repo.has_predictions(db),
    )


@router.get(
    "/stays/{stay_id}/vitals",
    response_model=VitalsResponse,
    summary="Vital 추이",
    description=(
        "생리학적으로 불가능한 값은 view 에서 NULL 로 처리되고, 체온은 섭씨로 변환된다. "
        "결측이 많으므로 프론트에서 선 끊김 처리가 필요하다."
    ),
    responses={404: {"description": "코호트에 없는 stay_id"}},
)
def get_stay_vitals(
    stay_id: int,
    db: Session = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
    order: str = Query("asc", description="asc | desc"),
) -> VitalsResponse:
    if order not in {"asc", "desc"}:
        raise HTTPException(status_code=400, detail="order must be 'asc' or 'desc'")
    if not repo.stay_exists(db, stay_id):
        raise HTTPException(status_code=404, detail="ED stay not found")

    rows = repo.list_vitals(db, stay_id, limit=limit, order=order)
    points = [svc.to_vital_point(r) for r in rows]
    newest = max(points, key=lambda p: p.measured_at) if points else None
    latest = (
        LatestVital(
            measured_at=newest.measured_at,
            heart_rate=newest.heart_rate,
            resp_rate=newest.resp_rate,
            sbp=newest.sbp,
            dbp=newest.dbp,
            spo2=newest.spo2,
            temperature_c=newest.temperature_c,
        )
        if newest
        else LatestVital()
    )
    return VitalsResponse(
        stay_id=str(stay_id), vitals=points, latest=latest, count=len(points), meta=VitalsMeta()
    )


@router.get(
    "/stays/{stay_id}/predictions",
    response_model=PredictionsResponse,
    summary="악화 예측 추이",
    description=(
        "모델이 연동되지 않은 상태에서는 predictions 가 빈 배열이고 "
        "meta.model_connected 가 false 다. 가짜 확률을 만들지 않는다."
    ),
    responses={404: {"description": "코호트에 없는 stay_id"}},
)
def get_stay_predictions(stay_id: int, db: Session = Depends(get_db)) -> PredictionsResponse:
    if not repo.stay_exists(db, stay_id):
        raise HTTPException(status_code=404, detail="ED stay not found")

    rows = repo.list_predictions(db, stay_id)
    return PredictionsResponse(
        stay_id=str(stay_id),
        predictions=[svc.to_prediction_point(r) for r in rows],
        latest=svc.latest_prediction(rows),
        count=len(rows),
        meta=PredictionMeta(model_connected=bool(rows)),
    )
