from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.patient import Patient
from app.models.visit import Visit
from app.schemas.visit import (
    VisitCreate,
    VisitResponse,
    VisitUpdate,
)


router = APIRouter(
    prefix="/api/visits",
    tags=["Visits"],
)


def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


@router.post(
    "",
    response_model=VisitResponse,
    status_code=201,
)
def create_visit(
    visit_data: VisitCreate,
    db: Session = Depends(get_db),
):
    # 환자 존재 여부 확인
    patient = db.get(Patient, visit_data.patient_id)

    if not patient:
        raise HTTPException(
            status_code=404,
            detail="Patient not found",
        )

    visit = Visit(
        patient_id=visit_data.patient_id,
        arrival_time=visit_data.arrival_time,
        triage_level=visit_data.triage_level,
        chief_complaint=visit_data.chief_complaint,
        status=visit_data.status,
    )

    db.add(visit)
    db.commit()
    db.refresh(visit)

    return visit


@router.get(
    "",
    response_model=list[VisitResponse],
)
def get_visits(
    status: str | None = None,
    db: Session = Depends(get_db),
):
    query = select(Visit).order_by(
        Visit.arrival_time.desc()
    )

    if status:
        query = query.where(
            Visit.status == status
        )

    visits = db.scalars(query).all()

    return visits


@router.get(
    "/{visit_id}",
    response_model=VisitResponse,
)
def get_visit(
    visit_id: int,
    db: Session = Depends(get_db),
):
    visit = db.get(Visit, visit_id)

    if not visit:
        raise HTTPException(
            status_code=404,
            detail="Visit not found",
        )

    return visit


@router.get(
    "/patient/{patient_id}",
    response_model=list[VisitResponse],
)
def get_patient_visits(
    patient_id: int,
    db: Session = Depends(get_db),
):
    patient = db.get(Patient, patient_id)

    if not patient:
        raise HTTPException(
            status_code=404,
            detail="Patient not found",
        )

    visits = db.scalars(
        select(Visit)
        .where(Visit.patient_id == patient_id)
        .order_by(Visit.arrival_time.desc())
    ).all()

    return visits


@router.put(
    "/{visit_id}",
    response_model=VisitResponse,
)
def update_visit(
    visit_id: int,
    visit_data: VisitUpdate,
    db: Session = Depends(get_db),
):
    visit = db.get(Visit, visit_id)

    if not visit:
        raise HTTPException(
            status_code=404,
            detail="Visit not found",
        )

    update_data = visit_data.model_dump(
        exclude_unset=True
    )

    for key, value in update_data.items():
        setattr(visit, key, value)

    db.commit()
    db.refresh(visit)

    return visit


@router.delete(
    "/{visit_id}",
    status_code=204,
)
def delete_visit(
    visit_id: int,
    db: Session = Depends(get_db),
):
    visit = db.get(Visit, visit_id)

    if not visit:
        raise HTTPException(
            status_code=404,
            detail="Visit not found",
        )

    db.delete(visit)
    db.commit()