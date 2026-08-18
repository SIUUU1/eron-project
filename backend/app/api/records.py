from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.record import Record
from app.models.visit import Visit
from app.schemas.record import (
    RecordCreate,
    RecordResponse,
    RecordUpdate,
)


router = APIRouter(
    prefix="/api",
    tags=["Records"],
)


def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


@router.post(
    "/visits/{visit_id}/records",
    response_model=RecordResponse,
    status_code=201,
)
def create_record(
    visit_id: int,
    record_data: RecordCreate,
    db: Session = Depends(get_db),
):
    visit = db.get(Visit, visit_id)

    if not visit:
        raise HTTPException(
            status_code=404,
            detail="Visit not found",
        )

    record = Record(
        visit_id=visit_id,
        record_type=record_data.record_type,
        content=record_data.content,
        generated_by=record_data.generated_by,
    )

    db.add(record)
    db.commit()
    db.refresh(record)

    return record


@router.get(
    "/visits/{visit_id}/records",
    response_model=list[RecordResponse],
)
def get_visit_records(
    visit_id: int,
    db: Session = Depends(get_db),
):
    visit = db.get(Visit, visit_id)

    if not visit:
        raise HTTPException(
            status_code=404,
            detail="Visit not found",
        )

    records = db.scalars(
        select(Record)
        .where(Record.visit_id == visit_id)
        .order_by(Record.created_at.desc())
    ).all()

    return records


@router.get(
    "/records/{record_id}",
    response_model=RecordResponse,
)
def get_record(
    record_id: int,
    db: Session = Depends(get_db),
):
    record = db.get(Record, record_id)

    if not record:
        raise HTTPException(
            status_code=404,
            detail="Record not found",
        )

    return record


@router.put(
    "/records/{record_id}",
    response_model=RecordResponse,
)
def update_record(
    record_id: int,
    record_data: RecordUpdate,
    db: Session = Depends(get_db),
):
    record = db.get(Record, record_id)

    if not record:
        raise HTTPException(
            status_code=404,
            detail="Record not found",
        )

    update_data = record_data.model_dump(
        exclude_unset=True
    )

    if "content" in update_data:
        record.content = update_data["content"]

    if "confirmed" in update_data:
        if update_data["confirmed"]:
            record.confirmed_at = datetime.utcnow()
        else:
            record.confirmed_at = None

    db.commit()
    db.refresh(record)

    return record


@router.delete(
    "/records/{record_id}",
    status_code=204,
)
def delete_record(
    record_id: int,
    db: Session = Depends(get_db),
):
    record = db.get(Record, record_id)

    if not record:
        raise HTTPException(
            status_code=404,
            detail="Record not found",
        )

    db.delete(record)
    db.commit()