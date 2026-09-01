from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.ed.common import Meta


class LatestVital(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    measured_at: datetime | None = None
    heart_rate: float | None = None
    resp_rate: float | None = None
    sbp: float | None = None
    dbp: float | None = None
    spo2: float | None = None
    temperature_c: float | None = Field(
        None, description="섭씨. MIMIC 원본은 화씨이며 조회 시 변환한다."
    )
    consciousness: str | None = Field(
        None,
        description=(
            "항상 null. MIMIC-IV-ED 테이블에 의식수준이 없다. "
            "GCS 는 ICU chartevents(39GB)에만 있어 이번 범위에서 적재하지 않았다."
        ),
    )


class EdStayListItem(BaseModel):
    stay_id: str
    display_name: str | None = Field(
        None,
        description=(
            "성씨 + 마스킹 표기(예: 김**). MIMIC 은 비식별화되어 이름이 없으며, "
            "성씨는 stay_id 해시로 배정한 가짜 값이다. 동일 성씨가 여러 명 나올 수 있고 "
            "식별은 stay_id 로 한다."
        ),
    )
    sex: str | None = Field(None, description='"M" | "F" 원본 값')
    age: int | None = None
    arrived_at: datetime | None = Field(None, description="데모 시간축이 적용된 내원 시각")
    acuity: int | None = Field(
        None, description="MIMIC triage.acuity = ESI 1~5. KTAS 와 동일 척도가 아니다."
    )
    chief_complaint: str | None = None
    chief_complaint_detail: str | None = None
    risk_level: str | None = Field(None, description="모델 미연동 시 null")
    risk_probability: float | None = Field(None, description="0.0~1.0. 모델 미연동 시 null")
    latest_vital: LatestVital
    bed_id: str | None = Field(None, description="데모 배정 (D2)")
    record_status: str | None = Field(None, description="DRAFT | SIGNED. 기록이 없으면 null")
    departed_at: datetime | None = Field(
        None,
        description=(
            "퇴실 시각(데모 시간축). 아직 퇴실 전이거나 outtime 이 없으면 null 이다."
        ),
    )
    discharge_type: str | None = Field(
        None,
        description=(
            "퇴실 유형. icu | admitted | home | expired. "
            "아직 퇴실 전이면 null. "
            "판정: disposition=EXPIRED → expired, ICU 이동 이력 있음 → icu, "
            "disposition=ADMITTED → admitted, disposition=HOME → home."
        ),
    )


class EdStayPage(BaseModel):
    items: list[EdStayListItem]
    page: int
    page_size: int
    total: int
    meta: Meta


class TriageSnapshot(BaseModel):
    heart_rate: float | None = None
    resp_rate: float | None = None
    sbp: float | None = None
    dbp: float | None = None
    spo2: float | None = None
    temperature_c: float | None = None
    pain: str | None = Field(
        None, description="문자열. 'unable', 'uta' 등 비수치 값이 존재한다."
    )


class HospitalInfo(BaseModel):
    hadm_id: str | None = None
    admitted: bool = False
    icu_transferred: bool = Field(False, description="ED 이후 ICU 이동 여부 (악화 결과 확인용)")


class EdStayDetail(BaseModel):
    stay_id: str
    subject_id_masked: str
    display_name: str | None = None
    sex: str | None = None
    age: int | None = None
    race: str | None = None
    arrived_at: datetime | None = None
    departed_at: datetime | None = None
    arrival_transport: str | None = None
    arrival_route: str | None = Field(
        None,
        description="admissions.admission_location. hadm_id 가 없으면 null (전체의 약 52%).",
    )
    acuity: int | None = None
    chief_complaint: str | None = None
    chief_complaint_detail: str | None = None
    triage: TriageSnapshot
    disposition: str | None = None
    hospital: HospitalInfo
    risk_level: str | None = None
    risk_probability: float | None = None
    bed_id: str | None = None
    meta: Meta
