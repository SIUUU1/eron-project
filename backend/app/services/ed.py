"""화면 단위 응답 조합.

· 데모 시간축 적용 (D6)
· 나이 계산 (anchor_age/anchor_year → 내원 시점 나이)
· 위험도 등급 산출, 예측이 없을 때의 대체 판정
raw SQL 결과를 Pydantic 스키마로 옮기는 곳이며, 여기서 직접 SQL 을 쓰지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.schemas.ed.common import Meta
from app.schemas.ed.dashboard import (
    AlertItem,
    BedItem,
    BedsMeta,
    BedSummary,
    BedZone,
    IncompleteRecordItem,
    ReassessItem,
)
from app.schemas.ed.prediction import LatestPrediction, PredictionPoint, RiskSignal
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
        risk_band=row["risk_band"],
        risk_probability=row["risk_probability"],
        alert_total=row["alert_total"],
        alert_unread=row["alert_unread"],
        # 재검토 필요 알림이 있고 전부 확인된 상태에서만 ✓ 를 띄운다.
        reviewed=bool(row["alert_total"]) and not row["alert_unread"],
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
        risk_band=row["risk_band"],
        risk_probability=row["risk_probability"],
        alert_total=row["alert_total"],
        alert_unread=row["alert_unread"],
        reviewed=bool(row["alert_total"]) and not row["alert_unread"],
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
    """최신 예측 1건 + 그 시점의 기여 신호.

    ⚠ 신호 문장은 riskmodel 이 만든 것을 그대로 옮긴다. 여기서 문구를 만들거나
      순서를 바꾸지 않는다 — 모델이 실제로 본 신호와 화면 문구가 어긋난다.
      권고(recommendations)는 이 모델이 생성하지 않으므로 항상 비어 있다.
    """
    if not rows:
        # 모델 미연동. 지어내지 않고 비운다.
        return LatestPrediction()
    last = rows[-1]
    detail = last["detail"] or {}
    signals = [RiskSignal(**s) for s in detail.get("reason_detail") or []]
    return LatestPrediction(
        risk_probability=last["risk_probability"],
        risk_level=last["risk_level"],
        risk_factors=[s.text for s in signals],
        risk_signals=signals,
        # ⚠ signals 가 비어도 reason_type/title 은 넘긴다. 임상 방향 gate 도입 후
        #   "위험도는 올랐지만 확인된 악화 신호가 없음" 이 정상 상태로 존재한다.
        reason_type=detail.get("reason_type"),
        reason_title=detail.get("reason_title"),
        reason_basis=detail.get("reason_basis"),
        clinical_worsening_confirmed=detail.get("clinical_worsening_confirmed"),
        reason_notice=detail.get("reason_notice"),
        risk_delta=detail.get("risk_delta"),
    )


# ------------------------------------------------------------------ dashboard

def to_bed_item(row: Any) -> tuple[BedItem, str]:
    """(병상, 구역) 을 돌려준다. 색은 모델 3구간, 아직 예측이 없으면 pending 이다."""
    if row["ed_stay_id"] is None:
        return BedItem(bed_id=row["bed_id"], status="empty"), row["zone"]

    band = row["risk_band"]
    return (
        BedItem(
            bed_id=row["bed_id"],
            status=risk.bed_status(band),
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
    counts = {"critical": 0, "moderate": 0, "low": 0, "pending": 0, "empty": 0}
    any_prediction = False

    for row in rows:
        # 예측이 하나라도 반영됐는지(화면 안내 문구용).
        if row["risk_band"] is not None:
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
    """색의 근거. 예측이 하나도 도래하지 않았으면 'none' 이다(대체 색을 쓰지 않는다)."""
    return BedsMeta(status_source="prediction" if any_prediction else "none")


# API 필드 ID와 저장된 record_payload.record 키의 대응. 기록 화면의 현재 필수 10개
# 항목과 같고, 진료계획·추정진단은 현재 필수 항목이 아니므로 포함하지 않는다.
_REQUIRED_RECORD_FIELDS = (
    ("chief_complaint", "chiefComplaint"),
    ("pain_assessment", "painAssessment"),
    ("history_of_present_illness", "presentIllness"),
    ("past_history", "pastHistory"),
    ("medications", "medication"),
    ("allergy", "allergy"),
    ("social_history", "socialHistory"),
    ("review_of_systems", "systemReview"),
    ("physical_examination", "physicalExam"),
    ("outcome", "outcome"),
)
_MISSING_RECORD_VALUES = {"", "미확인", "NOT_ASSESSED", "선택되지 않음"}


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _missing_required_fields(record_payload: Any) -> list[str]:
    payload = _mapping(record_payload)
    record = _mapping(payload.get("record"))
    statuses = _mapping(payload.get("field_statuses"))
    missing: list[str] = []

    for api_field, record_field in _REQUIRED_RECORD_FIELDS:
        status = statuses.get(record_field)
        if status == "missing":
            missing.append(api_field)
            continue
        if status in {"complete", "review"}:
            continue

        value = record.get(record_field)
        normalized = value.strip() if isinstance(value, str) else ""
        if normalized in _MISSING_RECORD_VALUES:
            missing.append(api_field)

    return missing


def to_incomplete_record_items(
    rows: list[Any], *, limit: int
) -> tuple[list[IncompleteRecordItem], int]:
    """재실 환자의 기록 미작성/필수 필드 누락 목록과 전체 건수를 만든다."""
    incomplete: list[IncompleteRecordItem] = []
    for row in rows:
        status = row["record_status"]
        if status == "SIGNED":
            continue
        if status is None:
            incomplete.append(
                IncompleteRecordItem(
                    stay_id=str(row["stay_id"]),
                    display_name=row["display_name"],
                    record_status=None,
                    reason="RECORD_NOT_CREATED",
                )
            )
            continue

        missing_fields = _missing_required_fields(row["record_payload"])
        if missing_fields:
            incomplete.append(
                IncompleteRecordItem(
                    stay_id=str(row["stay_id"]),
                    display_name=row["display_name"],
                    record_status="DRAFT",
                    reason="MISSING_REQUIRED_FIELDS",
                    missing_fields=missing_fields,
                )
            )

    return incomplete[:limit], len(incomplete)


def reassess_meta(any_prediction: bool) -> BedsMeta:
    """재평가 큐의 정렬 근거. 여기는 예측이 없을 때 ESI 중증도로 **대체 정렬**한다.

    병상 색(beds_meta)과 기준이 다르므로 meta 도 따로 만든다 — 병상은 근거가 없으면
    색을 칠하지 않고(pending), 큐는 그래도 순서를 매겨야 하기 때문이다.
    """
    return BedsMeta(status_source="prediction" if any_prediction else "triage_acuity")


# 설명이 비어 있을 때 쓰는 문구. 경보 사실만 말하고 임상 해석을 붙이지 않는다.
_ALARM_ONLY_MESSAGE = "모델 경보 임계값 초과"


def to_alert_item(row: Any) -> AlertItem:
    """경보 1건. message 는 모델이 만든 신호 문장을 그대로 쓴다."""
    return AlertItem(
        id=row["id"],
        stay_id=str(row["ed_stay_id"]),
        display_name=row["display_name"],
        alert_time=row["alert_time"],
        level=row["level"],
        band=row["band"],
        risk_probability=row["risk_probability"],
        message=row["reason"] or _ALARM_ONLY_MESSAGE,
        reason_type=row["reason_type"],
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
                risk_band=row["risk_band"],
                risk_probability=row["risk_probability"],
                bed_id=row["bed_id"],
                acuity=row["acuity"],
                due_minutes=minutes,
                due_label=label,
            )
        )
    return items, any_prediction
