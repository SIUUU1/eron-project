from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.vital import Vital
from app.models.visit import Visit
from app.schemas.vital import (
    VitalCreate,
    VitalResponse,
    VitalUpdate,
)


router = APIRouter(
    prefix="/api",
    tags=["Vitals"],
)


def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


@router.post(
    "/visits/{visit_id}/vitals",
    response_model=VitalResponse,
    status_code=201,
)
def create_vital(
    visit_id: int,
    vital_data: VitalCreate,
    db: Session = Depends(get_db),
):
    visit = db.get(Visit, visit_id)

    if not visit:
        raise HTTPException(
            status_code=404,
            detail="Visit not found",
        )

    vital = Vital(
        visit_id=visit_id,
        measured_at=vital_data.measured_at,
        heart_rate=vital_data.heart_rate,
        respiratory_rate=vital_data.respiratory_rate,
        systolic_bp=vital_data.systolic_bp,
        diastolic_bp=vital_data.diastolic_bp,
        temperature=vital_data.temperature,
        spo2=vital_data.spo2,
        consciousness=vital_data.consciousness,
    )

    db.add(vital)
    db.commit()
    db.refresh(vital)

    return vital


@router.get(
    "/visits/{visit_id}/vitals",
    response_model=list[VitalResponse],
)
def get_visit_vitals(
    visit_id: int,
    db: Session = Depends(get_db),
):
    visit = db.get(Visit, visit_id)

    if not visit:
        raise HTTPException(
            status_code=404,
            detail="Visit not found",
        )

    vitals = db.scalars(
        select(Vital)
        .where(Vital.visit_id == visit_id)
        .order_by(Vital.measured_at.asc())
    ).all()

    return vitals


@router.get(
    "/vitals/{vital_id}",
    response_model=VitalResponse,
)
def get_vital(
    vital_id: int,
    db: Session = Depends(get_db),
):
    vital = db.get(Vital, vital_id)

    if not vital:
        raise HTTPException(
            status_code=404,
            detail="Vital not found",
        )

    return vital


@router.put(
    "/vitals/{vital_id}",
    response_model=VitalResponse,
)
def update_vital(
    vital_id: int,
    vital_data: VitalUpdate,
    db: Session = Depends(get_db),
):
    vital = db.get(Vital, vital_id)

    if not vital:
        raise HTTPException(
            status_code=404,
            detail="Vital not found",
        )

    update_data = vital_data.model_dump(
        exclude_unset=True
    )

    for key, value in update_data.items():
        setattr(vital, key, value)

    db.commit()
    db.refresh(vital)

    return vital


@router.delete(
    "/vitals/{vital_id}",
    status_code=204,
)
def delete_vital(
    vital_id: int,
    db: Session = Depends(get_db),
):
    vital = db.get(Vital, vital_id)

    if not vital:
        raise HTTPException(
            status_code=404,
            detail="Vital not found",
        )

    db.delete(vital)
    db.commit()