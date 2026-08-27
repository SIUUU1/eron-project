"""애플리케이션 생성 데이터 (스키마 app).

· prediction  : 모델 output 구조가 확정되지 않아 최소 필드 + detail JSONB.
· patient_alias / demo_stay / bed* : 데모 스캐폴딩. MIMIC 에 없는 정보다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.ed_base import EdBase

SCHEMA = "app"


class Prediction(EdBase):
    """악화 예측 결과.

    TODO — 모델 output 구조 미확인 (저장소에 모델·inference 코드 없음).
      · outcome 별 확률 분리 여부 (respiratory / vasopressor / death / cpr)
      · risk_factors[] 스키마 (문자열 배열인지, 기여도 포함 객체인지)
      · recommendations[] 출처 (모델 산출인지 규칙 기반인지)
      · horizon_minutes 실제 값, t_idx 시간 간격
    확정 전까지는 detail JSONB 에 담고, 확정 후 실제 컬럼으로 승격한다.
    """

    __tablename__ = "prediction"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ed_stay_id: Mapped[int] = mapped_column(BigInteger)
    model_version: Mapped[str] = mapped_column(Text)
    prediction_time: Mapped[datetime] = mapped_column(DateTime)
    t_idx: Mapped[int | None] = mapped_column(Integer)
    horizon_minutes: Mapped[int | None] = mapped_column(Integer)
    risk_probability: Mapped[float] = mapped_column(Float)
    risk_level: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class PatientAlias(EdBase):
    """D1 확정 — 결정론적 가명. MIMIC 은 비식별화되어 이름이 없다."""

    __tablename__ = "patient_alias"
    __table_args__ = {"schema": SCHEMA}

    ed_stay_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text)
    is_pseudonym: Mapped[bool] = mapped_column(Boolean)


class DemoStay(EdBase):
    """D6 확정 — 데모 시간축.

    MIMIC 의 timestamp 는 환자별로 shift 되어 있어 "현재 재실" 개념이 없다.
    원천 값은 건드리지 않고, 원본 시간축에서 '현재' 에 대응하는 시점(now_ref)만
    저장한다. 오프셋은 조회할 때 now() - now_ref 로 계산한다(app.v_demo_stay).

    오프셋을 값으로 저장하면 적재 시점에 고정되어, 실제 시간이 흐를수록
    코호트 전체가 과거로 밀리고 "현재 재실 환자" 가 사라진다.
    """

    __tablename__ = "demo_stay"
    __table_args__ = {"schema": SCHEMA}

    ed_stay_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    now_ref: Mapped[datetime] = mapped_column(DateTime)
    is_active: Mapped[bool] = mapped_column(Boolean)


class Bed(EdBase):
    """D2 확정 — 병상. MIMIC 에 병상 배치 정보가 없어 데모 값이다."""

    __tablename__ = "bed"
    __table_args__ = {"schema": SCHEMA}

    bed_id: Mapped[str] = mapped_column(Text, primary_key=True)
    zone: Mapped[str] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer)


class BedAssignment(EdBase):
    __tablename__ = "bed_assignment"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bed_id: Mapped[str] = mapped_column(ForeignKey(f"{SCHEMA}.bed.bed_id"))
    ed_stay_id: Mapped[int | None] = mapped_column(BigInteger)
    devices: Mapped[list[str]] = mapped_column(ARRAY(Text))
    assigned_at: Mapped[datetime] = mapped_column(DateTime)
    released_at: Mapped[datetime | None] = mapped_column(DateTime)


class Alert(EdBase):
    """모델 연동 전까지 비어 있다. 가짜 경고를 만들지 않는다."""

    __tablename__ = "alert"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ed_stay_id: Mapped[int] = mapped_column(BigInteger)
    alert_time: Mapped[datetime] = mapped_column(DateTime)
    level: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    acknowledged_by: Mapped[str | None] = mapped_column(Text)
