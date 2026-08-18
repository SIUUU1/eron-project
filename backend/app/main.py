from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import engine
from app.models.base import Base
from app.models import (
    Patient,
    Visit,
    Vital,
    Prediction,
    Record,
)
from app.api.patients import router as patient_router
from app.api.visits import router as visit_router
from app.api.vitals import router as vital_router
from app.api.predictions import router as prediction_router
from app.api.records import router as record_router


Base.metadata.create_all(bind=engine)


app = FastAPI(
    title="ER:ON Backend",
    description="AI 기반 응급실 통합 지원시스템 Backend",
    version="0.1.0",
)


app.include_router(patient_router)
app.include_router(visit_router)
app.include_router(vital_router)
app.include_router(prediction_router)
app.include_router(record_router)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "eron-backend",
    }


@app.get("/health/db")
def health_db():
    with Session(engine) as session:
        result = session.execute(text("SELECT 1"))
        value = result.scalar()

    return {
        "status": "ok",
        "database": "connected",
        "result": value,
    }