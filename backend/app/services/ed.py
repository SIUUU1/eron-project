"""화면 단위 응답 조합.

· 데모 시간축 적용 (D6)
· 나이 계산 (anchor_age/anchor_year → 내원 시점 나이)
· 위험도 등급 산출, 예측이 없을 때의 대체 판정
raw SQL 결과를 Pydantic 스키마로 옮기는 곳이며, 여기서 직접 SQL 을 쓰지 않는다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.schemas.ed.common import Meta
from app.schemas.ed.dashboard import (
    AlertItem,
    BedItem,
    BedsMeta,
    BedSummary,
    BedZone,
    ReassessItem,
)
from app.schemas.ed.prediction import LatestPrediction, PredictionPoint
from app.schemas.ed.stay import (
    EdStayDetail,
    EdStayListItem,
    HospitalInfo,
    LatestVital,
    TriageSnapshot,
)
from app.schemas.ed.vitals import VitalPoint
from app.services import risk
from app.services.demo_time import age_at, shift


def build_meta(cohort_size: int | None = None, *, model_connected: bool = False) -> Meta:
    """model_connected 는 예측 데이터의 실제 유무다.

    PREDICT_AI_URL 이 설정돼 있어도 호출부가 없어 예측이 0건이면 false 다.
    프론트가 "위험도를 표시할 수 있는가" 를 이걸로 판단한다.
    """
    return Meta(cohort_size=cohort_size, model_connected=model_connected)


def mask_subject(subject_id: int | None) -> str:
    if subject_id is None:
        return "********"
    text = str(subject_id)
    keep = max(len(text) - 4, 0)
    return text[:keep] + "*" * (len(text) - keep)


def discharge_of(row: Any) -> tuple[datetime | None, str | None]:
    """(퇴실 시각, 퇴실 유형) 을 돌려준다.

    퇴실 여부 판정은 SQL(app.v_demo_stay.has_departed)이 한다.
    파이썬 프로세스와 DB 의 시계가 어긋나 경계에서 흔들리는 것을 피하기 위함이다.
    아직 재실 중이면 둘 다 None 을 돌려준다 (화면에서는 빈칸).
    """
    if not row["has_departed"]:
        return None, None

    departed = row["demo_outtime"]
    disposition = row["disposition"]
    if disposition == "EXPIRED":
        kind = "expired"
    elif row["icu_transferred"]:
        kind = "icu"
    elif disposition == "ADMITTED":
        kind = "admitted"
    elif disposition == "HOME":
        kind = "home"
    else:
        # 코호트는 ADMITTED / HOME / EXPIRED 로 한정되어 여기 오지 않는다.
        # 전체 적재로 넓히면 TRANSFER, ELOPED 등이 들어올 수 있어 유형은 비운다.
        return departed, None

    return departed, kind


def _latest_vital(row: Any) -> LatestVital:
    return LatestVital(
        measured_at=shift(row["measured_at"], row["demo_offset"]),
        heart_rate=row["heartrate"],
        resp_rate=row["resprate"],
        sbp=row["sbp"],
        dbp=row["dbp"],
        spo2=row["o2sat"],
        temperature_c=row["temperature_c"],
        consciousness=None,  # ED 테이블에 없음
    )


def to_list_item(row: Any) -> EdStayListItem:
    complaint = row["chiefcomplaint"]
    departed_at, discharge_type = discharge_of(row)
    return EdStayListItem(
        stay_id=str(row["stay_id"]),
        display_name=row["display_name"],
        sex=row["gender"],
        age=age_at(row["anchor_age"], row["anchor_year"], row["intime"]),
        arrived_at=row["demo_intime"],
        acuity=row["acuity"],
        chief_complaint=(complaint.split(",")[0].strip() if complaint else None),
        chief_complaint_detail=complaint,
        risk_level=row["risk_level"],
        risk_probability=row["risk_probability"],
        latest_vital=_latest_vital(row),
        bed_id=row["bed_id"],
        record_status=row["record_status"],
        departed_at=departed_at,
        discharge_type=discharge_type,
    )


def to_detail(row: Any, cohort_size: int | None, *, model_connected: bool = False) -> EdStayDetail:
    complaint = row["chiefcomplaint"]
    offset = row["demo_offset"]
    return EdStayDetail(
        stay_id=str(row["stay_id"]),
        subject_id_masked=mask_subject(row["subject_id"]),
        display_name=row["display_name"],
        sex=row["gender"],
        age=age_at(row["anchor_age"], row["anchor_year"], row["intime"]),
        race=row["race"],
        arrived_at=row["demo_intime"],
        departed_at=shift(row["outtime"], offset),
        arrival_transport=row["arrival_transport"],
        arrival_route=row["admission_location"],
        acuity=row["acuity"],
        chief_complaint=(complaint.split(",")[0].strip() if complaint else None),
        chief_complaint_detail=complaint,
        triage=TriageSnapshot(
            heart_rate=row["tri_hr"],
            resp_rate=row["tri_rr"],
            sbp=row["tri_sbp"],
            dbp=row["tri_dbp"],
            spo2=row["tri_spo2"],
            temperature_c=row["tri_temp_c"],
            pain=row["tri_pain"],
        ),
        disposition=row["disposition"],
        hospital=HospitalInfo(
            hadm_id=str(row["hadm_id"]) if row["hadm_id"] is not None else None,
            admitted=row["hadm_id"] is not None,
            icu_transferred=bool(row["icu_transferred"]),
        ),
        risk_level=row["risk_level"],
        risk_probability=row["risk_probability"],
        bed_id=row["bed_id"],
        meta=build_meta(cohort_size, model_connected=model_connected),
    )


def to_vital_point(row: Any) -> VitalPoint:
    measured = shift(row["charttime"], row["demo_offset"])
    assert measured is not None
    return VitalPoint(
        measured_at=measured,
        heart_rate=row["heartrate"],
        resp_rate=row["resprate"],
        sbp=row["sbp"],
        dbp=row["dbp"],
        spo2=row["o2sat"],
        temperature_c=row["temperature_c"],
        rhythm=row["rhythm"],
        pain=row["pain"],
        consciousness=None,
    )


def to_prediction_point(row: Any) -> PredictionPoint:
    moment = shift(row["prediction_time"], row["demo_offset"])
    assert moment is not None
    return PredictionPoint(
        prediction_time=moment,
        t_idx=row["t_idx"],
        horizon_minutes=row["horizon_minutes"],
        risk_probability=row["risk_probability"],
        risk_level=row["risk_level"],
        model_version=row["model_version"],
    )


def latest_prediction(rows: list[Any]) -> LatestPrediction:
    if not rows:
        # 모델 미연동. 지어내지 않고 비운다.
        return LatestPrediction()
    last = rows[-1]
    detail = last["detail"] or {}
    return LatestPrediction(
        risk_probability=last["risk_probability"],
        risk_level=last["risk_level"],
        # TODO — 모델 output 구조 확정 시 detail 에서 정식 필드로 승격
        risk_factors=list(detail.get("risk_factors", [])),
        recommendations=list(detail.get("recommendations", [])),
    )


# ------------------------------------------------------------------ dashboard

def to_bed_item(row: Any) -> tuple[BedItem, str]:
    """(병상, 구역) 을 돌려준다. 예측이 없으면 acuity 로 색을 정한다."""
    if row["ed_stay_id"] is None:
        return BedItem(bed_id=row["bed_id"], status="empty"), row["zone"]

    level = row["risk_level"] or risk.level_from_acuity(row["acuity"])
    return (
        BedItem(
            bed_id=row["bed_id"],
            status=risk.bed_status(level),
            stay_id=str(row["ed_stay_id"]),
            display_name=row["display_name"],
            age=age_at(row["anchor_age"], row["anchor_year"], row["intime"]),
            sex=row["gender"],
            devices=list(row["devices"] or []),
        ),
        row["zone"],
    )


def build_bed_zones(rows: list[Any]) -> tuple[list[BedZone], BedSummary, bool]:
    zones: list[BedZone] = []
    index: dict[str, BedZone] = {}
    counts = {"critical": 0, "moderate": 0, "low": 0, "empty": 0}
    any_prediction = False

    for row in rows:
        if row["risk_level"] is not None:
            any_prediction = True
        bed, zone_name = to_bed_item(row)
        counts[bed.status] = counts.get(bed.status, 0) + 1
        if zone_name not in index:
            zone = BedZone(zone=zone_name, beds=[])
            index[zone_name] = zone
            zones.append(zone)
        index[zone_name].beds.append(bed)

    summary = BedSummary(total=len(rows), **counts)
    return zones, summary, any_prediction


def beds_meta(any_prediction: bool) -> BedsMeta:
    return BedsMeta(status_source="prediction" if any_prediction else "triage_acuity")


def to_alert_item(row: Any) -> AlertItem:
    return AlertItem(
        id=row["id"],
        stay_id=str(row["ed_stay_id"]),
        display_name=row["display_name"],
        alert_time=row["alert_time"],
        level=row["level"],
        message=row["message"],
        acknowledged_at=row["acknowledged_at"],
    )


def to_reassess_items(rows: list[Any]) -> tuple[list[ReassessItem], bool]:
    items: list[ReassessItem] = []
    any_prediction = False
    for row in rows:
        level = row["risk_level"]
        if level is not None:
            any_prediction = True
        else:
            level = risk.level_from_acuity(row["acuity"])
        due = risk.due_for(level)
        if due is None:
            continue
        minutes, label = due
        items.append(
            ReassessItem(
                stay_id=str(row["stay_id"]),
                display_name=row["display_name"],
                risk_level=row["risk_level"],
                risk_probability=row["risk_probability"],
                acuity=row["acuity"],
                due_minutes=minutes,
                due_label=label,
            )
        )
    return items, any_prediction
