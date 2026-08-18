from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.prediction import Prediction
from app.models.visit import Visit
from app.schemas.prediction import (
    PredictionCreate,
    PredictionResponse,
)


router = APIRouter(
    prefix="/api",
    tags=["Predictions"],
)


def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


@router.post(
    "/visits/{visit_id}/predictions",
    response_model=PredictionResponse,
    status_code=201,
)
def create_prediction(
    visit_id: int,
    prediction_data: PredictionCreate,
    db: Session = Depends(get_db),
):
    visit = db.get(Visit, visit_id)

    if not visit:
        raise HTTPException(
            status_code=404,
            detail="Visit not found",
        )

    if not 0 <= prediction_data.risk_score <= 1:
        raise HTTPException(
            status_code=400,
            detail="risk_score must be between 0 and 1",
        )

    prediction = Prediction(
        visit_id=visit_id,
        risk_score=prediction_data.risk_score,
        risk_level=prediction_data.risk_level,
        prediction_horizon=prediction_data.prediction_horizon,
        risk_factors=prediction_data.risk_factors,
    )

    db.add(prediction)
    db.commit()
    db.refresh(prediction)

    return prediction


@router.get(
    "/visits/{visit_id}/predictions",
    response_model=list[PredictionResponse],
)
def get_visit_predictions(
    visit_id: int,
    db: Session = Depends(get_db),
):
    visit = db.get(Visit, visit_id)

    if not visit:
        raise HTTPException(
            status_code=404,
            detail="Visit not found",
        )

    predictions = db.scalars(
        select(Prediction)
        .where(Prediction.visit_id == visit_id)
        .order_by(Prediction.predicted_at.desc())
    ).all()

    return predictions


@router.get(
    "/predictions/{prediction_id}",
    response_model=PredictionResponse,
)
def get_prediction(
    prediction_id: int,
    db: Session = Depends(get_db),
):
    prediction = db.get(Prediction, prediction_id)

    if not prediction:
        raise HTTPException(
            status_code=404,
            detail="Prediction not found",
        )

    return prediction