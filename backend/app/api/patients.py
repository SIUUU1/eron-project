from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.patient import Patient
from app.schemas.patient import (
    PatientCreate,
    PatientResponse,
    PatientUpdate,
)


router = APIRouter(
    prefix="/api/patients",
    tags=["Patients"],
)


def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


@router.post(
    "",
    response_model=PatientResponse,
    status_code=201,
)
def create_patient(
    patient_data: PatientCreate,
    db: Session = Depends(get_db),
):
    existing_patient = db.scalar(
        select(Patient).where(
            Patient.patient_number == patient_data.patient_number
        )
    )

    if existing_patient:
        raise HTTPException(
            status_code=409,
            detail="Patient number already exists",
        )

    patient = Patient(
        patient_number=patient_data.patient_number,
        name=patient_data.name,
        birth_date=patient_data.birth_date,
        gender=patient_data.gender,
    )

    db.add(patient)
    db.commit()
    db.refresh(patient)

    return patient


@router.get(
    "",
    response_model=list[PatientResponse],
)
def get_patients(
    db: Session = Depends(get_db),
):
    patients = db.scalars(
        select(Patient).order_by(Patient.id.desc())
    ).all()

    return patients


@router.get(
    "/{patient_id}",
    response_model=PatientResponse,
)
def get_patient(
    patient_id: int,
    db: Session = Depends(get_db),
):
    patient = db.get(Patient, patient_id)

    if not patient:
        raise HTTPException(
            status_code=404,
            detail="Patient not found",
        )

    return patient


@router.put(
    "/{patient_id}",
    response_model=PatientResponse,
)
def update_patient(
    patient_id: int,
    patient_data: PatientUpdate,
    db: Session = Depends(get_db),
):
    patient = db.get(Patient, patient_id)

    if not patient:
        raise HTTPException(
            status_code=404,
            detail="Patient not found",
        )

    update_data = patient_data.model_dump(
        exclude_unset=True
    )

    for key, value in update_data.items():
        setattr(patient, key, value)

    db.commit()
    db.refresh(patient)

    return patient


@router.delete(
    "/{patient_id}",
    status_code=204,
)
def delete_patient(
    patient_id: int,
    db: Session = Depends(get_db),
):
    patient = db.get(Patient, patient_id)

    if not patient:
        raise HTTPException(
            status_code=404,
            detail="Patient not found",
        )

    db.delete(patient)
    db.commit()