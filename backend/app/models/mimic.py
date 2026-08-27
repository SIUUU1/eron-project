"""MIMIC-IV 원천 서브셋 (스키마 mimic). 읽기 전용."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Float, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.ed_base import EdBase

SCHEMA = "mimic"


class MimicPatient(EdBase):
    __tablename__ = "patients"
    __table_args__ = {"schema": SCHEMA}

    subject_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    gender: Mapped[str | None] = mapped_column(String(1))
    anchor_age: Mapped[int | None] = mapped_column(SmallInteger)
    anchor_year: Mapped[int | None] = mapped_column(SmallInteger)
    anchor_year_group: Mapped[str | None] = mapped_column(Text)
    dod: Mapped[date | None] = mapped_column(Date)


class MimicAdmission(EdBase):
    __tablename__ = "admissions"
    __table_args__ = {"schema": SCHEMA}

    hadm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subject_id: Mapped[int] = mapped_column(BigInteger)
    admittime: Mapped[datetime | None] = mapped_column(DateTime)
    dischtime: Mapped[datetime | None] = mapped_column(DateTime)
    deathtime: Mapped[datetime | None] = mapped_column(DateTime)
    admission_type: Mapped[str | None] = mapped_column(Text)
    admission_location: Mapped[str | None] = mapped_column(Text)
    discharge_location: Mapped[str | None] = mapped_column(Text)
    insurance: Mapped[str | None] = mapped_column(Text)
    marital_status: Mapped[str | None] = mapped_column(Text)
    race: Mapped[str | None] = mapped_column(Text)
    edregtime: Mapped[datetime | None] = mapped_column(DateTime)
    edouttime: Mapped[datetime | None] = mapped_column(DateTime)
    hospital_expire_flag: Mapped[int | None] = mapped_column(SmallInteger)


class EdStay(EdBase):
    __tablename__ = "edstays"
    __table_args__ = {"schema": SCHEMA}

    stay_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subject_id: Mapped[int] = mapped_column(BigInteger)
    hadm_id: Mapped[int | None] = mapped_column(BigInteger)
    intime: Mapped[datetime] = mapped_column(DateTime)
    outtime: Mapped[datetime | None] = mapped_column(DateTime)
    gender: Mapped[str | None] = mapped_column(String(1))
    race: Mapped[str | None] = mapped_column(Text)
    arrival_transport: Mapped[str | None] = mapped_column(Text)
    disposition: Mapped[str | None] = mapped_column(Text)


class Triage(EdBase):
    __tablename__ = "triage"
    __table_args__ = {"schema": SCHEMA}

    stay_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subject_id: Mapped[int] = mapped_column(BigInteger)
    temperature: Mapped[float | None] = mapped_column(Float)  # °F
    heartrate: Mapped[float | None] = mapped_column(Float)
    resprate: Mapped[float | None] = mapped_column(Float)
    o2sat: Mapped[float | None] = mapped_column(Float)
    sbp: Mapped[float | None] = mapped_column(Float)
    dbp: Mapped[float | None] = mapped_column(Float)
    pain: Mapped[str | None] = mapped_column(Text)  # 자유텍스트 존재
    acuity: Mapped[int | None] = mapped_column(SmallInteger)  # ESI 1~5
    chiefcomplaint: Mapped[str | None] = mapped_column(Text)


class EdVitalsign(EdBase):
    __tablename__ = "ed_vitalsign"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    stay_id: Mapped[int] = mapped_column(BigInteger)
    subject_id: Mapped[int] = mapped_column(BigInteger)
    charttime: Mapped[datetime] = mapped_column(DateTime)
    temperature: Mapped[float | None] = mapped_column(Float)  # °F
    heartrate: Mapped[float | None] = mapped_column(Float)
    resprate: Mapped[float | None] = mapped_column(Float)
    o2sat: Mapped[float | None] = mapped_column(Float)
    sbp: Mapped[float | None] = mapped_column(Float)
    dbp: Mapped[float | None] = mapped_column(Float)
    rhythm: Mapped[str | None] = mapped_column(Text)
    pain: Mapped[str | None] = mapped_column(Text)


class EdDiagnosis(EdBase):
    __tablename__ = "ed_diagnosis"
    __table_args__ = {"schema": SCHEMA}

    stay_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    seq_num: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    subject_id: Mapped[int] = mapped_column(BigInteger)
    icd_code: Mapped[str] = mapped_column(Text)
    icd_version: Mapped[int] = mapped_column(SmallInteger)
    icd_title: Mapped[str | None] = mapped_column(Text)


class IcuStay(EdBase):
    __tablename__ = "icustays"
    __table_args__ = {"schema": SCHEMA}

    icu_stay_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subject_id: Mapped[int] = mapped_column(BigInteger)
    hadm_id: Mapped[int] = mapped_column(BigInteger)
    first_careunit: Mapped[str | None] = mapped_column(Text)
    last_careunit: Mapped[str | None] = mapped_column(Text)
    intime: Mapped[datetime | None] = mapped_column(DateTime)
    outtime: Mapped[datetime | None] = mapped_column(DateTime)
    los: Mapped[float | None] = mapped_column(Float)
