"""데모 시계 제어.

/api/ed/* 는 원칙적으로 읽기 전용이지만, 이 라우터만 예외로 쓰기를 허용한다.
쓰기 대상은 app.demo_clock 한 행뿐이며 MIMIC 원천 데이터는 건드리지 않는다.

시연 용도: 1시간 단위 악화 예측을 보여주기 위해 데모 시각을 빠르게 진행한다.
시계를 움직이면 목록·상세·차트·병상·퇴실 판정이 한꺼번에 따라온다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.repositories import demo_clock as repo
from app.schemas.ed.demo import DemoClock

router = APIRouter(prefix="/api/ed/demo", tags=["ED Demo Clock"])

MAX_STEP_HOURS = 72.0
MAX_SPEED = 86_400.0  # 실제 1초 = 데모 1일


def _to_schema(row) -> DemoClock:
    offset = int(row["offset_seconds"])
    elapsed = int(row["elapsed_seconds"])
    return DemoClock(
        virtual_now=row["virtual_now"],
        real_now=row["real_now"],
        speed=float(row["speed"]),
        offset_seconds=offset,
        elapsed_seconds=elapsed,
        can_rewind=elapsed > 60,
        # 1초 미만 오차는 흐름으로 보지 않는다
        is_shifted=abs(offset) > 1 or float(row["speed"]) != 1.0,
    )


@router.get("/clock", response_model=DemoClock, summary="데모 시계 상태")
def get_clock(db: Session = Depends(get_db)) -> DemoClock:
    return _to_schema(repo.read(db))


@router.post(
    "/advance",
    response_model=DemoClock,
    summary="데모 시각 진행",
    description=(
        "가상 시각을 지정한 시간만큼 앞당긴다. 음수면 되감는다. "
        "되감기는 시나리오 시작점보다 이전으로는 내려가지 않는다."
    ),
    responses={400: {"description": "허용 범위를 벗어난 값"}},
)
def advance(
    hours: float = Query(1.0, description="진행할 시간. 음수 가능"),
    db: Session = Depends(get_db),
) -> DemoClock:
    if not -MAX_STEP_HOURS <= hours <= MAX_STEP_HOURS:
        raise HTTPException(
            status_code=400, detail=f"hours must be between -{MAX_STEP_HOURS} and {MAX_STEP_HOURS}"
        )
    return _to_schema(repo.advance(db, hours))


@router.post(
    "/speed",
    response_model=DemoClock,
    summary="데모 배속 변경",
    description="0=정지, 1=실시간, 3600=실제 1초에 데모 1시간.",
    responses={400: {"description": "허용 범위를 벗어난 값"}},
)
def set_speed(
    value: float = Query(..., ge=0, description="배속"),
    db: Session = Depends(get_db),
) -> DemoClock:
    if value > MAX_SPEED:
        raise HTTPException(status_code=400, detail=f"speed must be <= {MAX_SPEED}")
    return _to_schema(repo.set_speed(db, value))


@router.post(
    "/reset",
    response_model=DemoClock,
    summary="데모 시계 초기화",
    description=(
        "실제 시각·실시간 속도로 되돌리고, **의료진 재검토 확인 기록(app.prediction_ack)을 "
        "비운다** — 지난 시연의 확인 상태가 새 시연에 남지 않게 한다. "
        "예측 결과(app.prediction)와 MIMIC 원천 데이터는 지우지 않는다."
    ),
)
def reset(db: Session = Depends(get_db)) -> DemoClock:
    return _to_schema(repo.reset(db))
