from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import engine
from app.models.base import Base
from app.models import (
    Patient,
    Visit,
    Vital,
    Prediction,
    Record,
    ClinicalRecord,
    KcdCode,
)
from app.api.patients import router as patient_router
from app.api.visits import router as visit_router
from app.api.vitals import router as vital_router
from app.api.predictions import router as prediction_router
from app.api.records import router as record_router
from app.api.ed_stays import router as ed_stay_router
from app.api.ed_dashboard import router as ed_dashboard_router
from app.api.ed_demo import router as ed_demo_router
from app.api.clinical_records import router as clinical_record_router
from app.api.kcd import router as kcd_router


# public 스키마의 기존 CRUD 도메인만 생성한다.
# mimic / app 스키마는 database/init/*.sql 과 적재 스크립트가 만든다 (EdBase 는 별도 메타데이터).
Base.metadata.create_all(bind=engine)


app = FastAPI(
    title="ER:ON Backend",
    description="AI 기반 응급실 통합 지원시스템 Backend",
    version="0.1.0",
)


# nginx 뒤에서는 프론트와 API 가 동일 오리진이라 CORS 가 필요 없다.
# 컨테이너 밖에서 vite dev 를 띄울 때만 CORS_ORIGINS 를 채운다.
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )


# 기존 CRUD 도메인 (경로·응답 형태 유지)
app.include_router(patient_router)
app.include_router(visit_router)
app.include_router(vital_router)
app.include_router(prediction_router)
app.include_router(record_router)
app.include_router(clinical_record_router)
app.include_router(kcd_router)

# MIMIC 기반 조회 (신규 네임스페이스, 읽기 전용)
app.include_router(ed_stay_router)
app.include_router(ed_dashboard_router)

# 데모 시계 제어 (app.demo_clock 한 행만 쓰기)
app.include_router(ed_demo_router)


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
