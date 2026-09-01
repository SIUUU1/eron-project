from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import math
import re
import time
from typing import Any, Iterable

from .field_routing_policy import (
    CANONICAL_TO_DRAFT_FIELD,
    FIELD_POLICIES,
    SYMPTOM,
    candidate_term_types,
    choose_evidence_field,
    evidence_fields_by_segment,
    fallback_field_for_term_type,
    field_collection_hints_by_segment,
    filter_candidates_for_field,
)
from .pipeline import run_api3


SCHEMA_VERSION = "clinical-workflow-v1"
DRAFT_FIELD_IDS = (
    "chief",
    "pain",
    "history",
    "past-history",
    "medication",
    "allergy",
    "social",
    "review-of-systems",
    "physical",
    "impression",
    "treatment-plan",
)
_CLINICIAN_REVIEW_ONLY_MATCH_TYPES = frozenset(
    {
        "approved_alias_candidate",
        "medical_query_raw_exact",
        "raw_similarity_candidate",
        "umls_dictionary_search",
        "ngram_dictionary_fallback",
    }
)


_SUPPORTED_FIELD_SOURCES = {
    "chief": "chief_complaint",
    "pain": "pain_assessment",
    "history": "history_of_present_illness",
    "past-history": "past_history",
    "medication": "medications",
    "allergy": "allergy",
    "social": "social_history",
    "review-of-systems": "review_of_systems",
    "physical": "physical_examination",
    "impression": "impression",
    "treatment-plan": "treatment_plan",
}
_CANONICAL_TO_LEGACY_FIELD_ID = {
    canonical: legacy for legacy, canonical in _SUPPORTED_FIELD_SOURCES.items()
}

_CLINICAL_NOTE_ACTION_STEMS = (
    "증가",
    "감소",
    "악화",
    "호전",
    "발생",
    "시행",
    "확인",
    "관찰",
    "투여",
    "복용",
    "호소",
    "보고",
    "진단",
    "측정",
    "내원",
    "입원",
    "퇴원",
    "전원",
)
_CLINICAL_NOTE_ENDING = re.compile(r"(?=\s*(?:[.!]|$))", re.MULTILINE)


def _clinical_note_style(value: Any) -> str:
    """Format display-only draft prose without changing source evidence."""

    text = str(value or "")
    if not text:
        return text
    action_stems = "|".join(map(re.escape, _CLINICAL_NOTE_ACTION_STEMS))
    replacements = (
        (r"보였습니다", "보였음"),
        (r"보입니다", "보임"),
        (r"있었습니다", "있었음"),
        (r"없었습니다", "없었음"),
        (r"있습니다", "있음"),
        (r"없습니다", "없음"),
        (rf"(?P<stem>{action_stems})했습니다", r"\g<stem>함"),
        (r"변했습니다", "변했음"),
        (rf"(?P<stem>{action_stems})(?:되었|됐)습니다", r"\g<stem>됨"),
        (r"되었습니다", "되었음"),
        (r"됐습니다", "됐음"),
        (r"했습니다", "했음"),
        (r"됩니다", "됨"),
        (r"입니다", "임"),
        (r"합니다", "함"),
        (r"였습니다", "였음"),
        (r"았습니다", "았음"),
        (r"었습니다", "었음"),
        (r"아픕니다", "아픔"),
        (r"빠릅니다", "빠름"),
        (r"느립니다", "느림"),
        (r"나쁩니다", "나쁨"),
        (r"납니다", "남"),
        (r"갑니다", "감"),
        (r"옵니다", "옴"),
        (r"봅니다", "봄"),
        (r"줍니다", "줌"),
        (r"둡니다", "둠"),
        (r"씁니다", "씀"),
        (r"습니다", "음"),
    )
    for ending, replacement in replacements:
        text = re.sub(ending + _CLINICAL_NOTE_ENDING.pattern, replacement, text, flags=re.MULTILINE)
    return text

def _is_question_text(text: Any) -> bool:
    normalized = str(text or "").strip()
    if "?" in normalized:
        return not bool(normalized.rsplit("?", 1)[1].strip())
    return bool(
        re.search(
            r"(?:있나요|없나요|하나요|인가요|셨나요|했나요|가요|까요|습니까|"
            r"있으세요|없으세요|되나요)[.!]?\s*$",
            normalized,
        )
    )


def _candidate_assertion(
    segment: dict[str, Any],
    source_span: dict[str, Any],
) -> str:
    text = str(segment.get("raw_text") or "")
    if _is_question_text(text):
        return "question_only"
    start = source_span.get("start_char")
    end = source_span.get("end_char")
    if not isinstance(start, int) or not isinstance(end, int):
        relevant = text
    else:
        relevant = text[max(0, start) : min(len(text), end + 30)]
    if re.search(r"모르|확실하지|불확실|것\s*같|기억(?:이)?\s*안", relevant):
        return "uncertain"
    if re.search(r"(?:않|없|아니|못)(?:았|었|하|해|습|는|다|요|고|은|는)?", relevant):
        return "negated"
    return "affirmed"


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, {}):
        return []
    return [value]


def _normalize_api2_document(
    api2_document: dict[str, Any],
    api3_document: dict[str, Any],
    *,
    preserve_unsupported: bool = False,
) -> dict[str, Any]:
    normalized = copy.deepcopy(api2_document)
    record = normalized.get("clinical_record")
    if not isinstance(record, dict):
        record = {}
        normalized["clinical_record"] = record
    segments = [
        segment
        for segment in api3_document.get("segments", [])
        if isinstance(segment, dict)
    ]

    def evidence_segment(value: dict[str, Any]) -> dict[str, Any] | None:
        evidence = value.get("evidence")
        if not isinstance(evidence, dict):
            return None
        segment_id = evidence.get("source_segment_id")
        if segment_id is not None:
            return next(
                (segment for segment in segments if segment.get("id") == segment_id),
                None,
            )
        start, end = evidence.get("start"), evidence.get("end")
        return next(
            (
                segment
                for segment in segments
                if segment.get("start") == start and segment.get("end") == end
            ),
            None,
        )

    discarded = object()
    warnings = normalized.setdefault("validation_warnings", [])
    if not isinstance(warnings, list):
        warnings = []
        normalized["validation_warnings"] = warnings

    def grounded(value: Any) -> Any:
        if isinstance(value, list):
            return [
                cleaned
                for item in value
                if (cleaned := grounded(item)) is not discarded
            ]
        if not isinstance(value, dict):
            return value
        if value.get("status") in {"confirmed", "needs_confirmation"}:
            raw_value = value.get("raw_value")
            segment = evidence_segment(value)
            if not isinstance(raw_value, str) or not raw_value.strip() or segment is None:
                warnings.append("ungrounded clinical value requires validation")
                return copy.deepcopy(value) if preserve_unsupported else discarded
            source_text = str(segment.get("raw_text") or "")
            if _is_question_text(source_text):
                warnings.append("clinical value supported only by a question requires validation")
                return copy.deepcopy(value) if preserve_unsupported else discarded
            compact_value = "".join(raw_value.split())
            compact_source = "".join(source_text.split())
            if compact_value not in compact_source:
                warnings.append("clinical value not found in source segment requires validation")
                return copy.deepcopy(value) if preserve_unsupported else discarded
            cleaned_value = copy.deepcopy(value)
            cleaned_evidence = copy.deepcopy(cleaned_value.get("evidence") or {})
            cleaned_evidence["source_segment_id"] = segment.get("id")
            cleaned_value["evidence"] = cleaned_evidence
            proposed_normalization = cleaned_value.pop("normalized_value", None)
            if (
                isinstance(proposed_normalization, str)
                and proposed_normalization.strip()
                and proposed_normalization.strip() != raw_value.strip()
            ):
                cleaned_value["normalization_provenance"] = {
                    "basis_type": "UNVERIFIED_MODEL_OUTPUT",
                    "status": "REVIEW_REQUIRED",
                    "proposed_value": proposed_normalization.strip(),
                }
                warnings.append("unverified model normalization requires review")
            return cleaned_value
        return {
            key: cleaned
            for key, item in value.items()
            if (cleaned := grounded(item)) is not discarded
        }

    cleaned_record = grounded(record)
    record = cleaned_record if isinstance(cleaned_record, dict) else {}
    normalized["clinical_record"] = record
    history = record.get("history_of_present_illness")
    if not isinstance(history, dict):
        history = {}
        record["history_of_present_illness"] = history
    history["associated_symptoms"] = _as_list(history.get("associated_symptoms"))
    for field_name in (
        "review_of_systems",
        "physical_examination",
        "impression",
        "treatment_plan",
    ):
        record[field_name] = _as_list(record.get(field_name))
    segment_positions = {
        segment.get("id"): position for position, segment in enumerate(segments)
    }
    retained_associated: list[Any] = []
    for item in history["associated_symptoms"]:
        source_segment = evidence_segment(item) if isinstance(item, dict) else None
        source_position = (
            segment_positions.get(source_segment.get("id"))
            if source_segment is not None
            else None
        )
        previous_text = (
            str(segments[source_position - 1].get("raw_text") or "")
            if isinstance(source_position, int) and source_position > 0
            else ""
        )
        is_symptom_review_answer = _is_question_text(previous_text) and any(
            term in previous_text
            for term in (
                "증상",
                "열",
                "기침",
                "소변",
                "구토",
                "설사",
                "어지",
                "숨",
                "가슴",
            )
        )
        if is_symptom_review_answer:
            if item not in record["review_of_systems"]:
                record["review_of_systems"].append(item)
        else:
            retained_associated.append(item)
    history["associated_symptoms"] = retained_associated

    ros_values = {
        str(item.get("raw_value") or "")
        for item in record["review_of_systems"]
        if isinstance(item, dict)
    }
    symptom_terms = ("소변", "기침", "열", "구토", "설사", "어지", "숨", "가슴", "통증")
    uncertainty_pattern = r"(?:모르|확실|것 같|기억)"
    for position, segment in enumerate(segments):
        question = str(segment.get("raw_text") or "")
        if not (
            _is_question_text(question)
            and any(term in question for term in symptom_terms)
        ):
            continue
        for answer in segments[position + 1 : position + 3]:
            if _is_question_text(answer.get("raw_text")):
                break
            answer_text = str(answer.get("raw_text") or "")
            if not re.search(uncertainty_pattern, answer_text):
                continue
            for term in symptom_terms:
                if term not in question or term not in answer_text:
                    continue
                match = re.search(
                    rf"{re.escape(term)}[^,.!?]*{uncertainty_pattern}[^,.!?]*[.!?]?",
                    answer_text,
                )
                raw_value = match.group(0).strip() if match else ""
                if not raw_value or raw_value in ros_values:
                    continue
                record["review_of_systems"].append(
                    {
                        "raw_value": raw_value,
                        "status": "needs_confirmation",
                        "evidence": {
                            "text": answer_text,
                            "start": answer.get("start"),
                            "end": answer.get("end"),
                        },
                    }
                )
                ros_values.add(raw_value)
    past_history = record.get("past_history")
    if not isinstance(past_history, dict):
        past_history = {}
        record["past_history"] = past_history
    past_history["underlying_conditions"] = _as_list(
        past_history.get("underlying_conditions")
    )
    past_history["surgery_history"] = _as_list(
        past_history.get("surgery_history")
    )
    retained_surgeries: list[Any] = []
    for item in past_history["surgery_history"]:
        raw_value = (
            str(item.get("raw_value") or "") if isinstance(item, dict) else ""
        )
        is_surgery = any(
            term in raw_value for term in ("수술", "절제", "시술", "이식")
        )
        is_disease_history = any(
            term in raw_value for term in ("진단", "질환", "병", "앓")
        )
        if is_disease_history and not is_surgery:
            past_history["underlying_conditions"].append(item)
        else:
            retained_surgeries.append(item)
    past_history["surgery_history"] = retained_surgeries

    existing_history_values = {
        str(item.get("raw_value") or "")
        for key in ("underlying_conditions", "surgery_history")
        for item in past_history[key]
        if isinstance(item, dict)
    }
    for position, segment in enumerate(segments):
        question = str(segment.get("raw_text") or "")
        is_history_question = _is_question_text(question) and any(
            term in question for term in ("앓고", "병력", "진단받", "수술")
        )
        if not is_history_question:
            continue
        for answer in segments[position + 1 : position + 3]:
            if _is_question_text(answer.get("raw_text")):
                break
            answer_text = str(answer.get("raw_text") or "").strip()
            if not answer_text or answer_text in existing_history_values:
                continue
            has_disease_history = any(
                term in answer_text for term in ("진단", "질환", "병", "앓")
            )
            has_surgery_history = any(
                term in answer_text for term in ("수술", "절제", "시술", "이식")
            )
            target = (
                "surgery_history"
                if has_surgery_history
                else "underlying_conditions" if has_disease_history else None
            )
            if target is None:
                continue
            uncertain = any(
                term in answer_text for term in ("모르", "기억", "것 같", "확실")
            )
            past_history[target].append(
                {
                    "raw_value": answer_text,
                    "status": "needs_confirmation" if uncertain else "confirmed",
                    "evidence": {
                        "text": answer_text,
                        "start": answer.get("start"),
                        "end": answer.get("end"),
                    },
                }
            )
            existing_history_values.add(answer_text)

    def grounded_value(
        segment: dict[str, Any],
        raw_value: str,
        normalized_value: str,
        *,
        status: str = "confirmed",
        rule_id: str = "N01_APPROVED_LOCAL_RULE",
        basis_type: str = "APPROVED_RULE",
    ) -> dict[str, Any]:
        return {
            "raw_value": raw_value,
            "normalized_value": normalized_value,
            "normalization_provenance": {
                "basis_type": basis_type,
                "status": "APPROVED",
                "rule_id": rule_id,
            },
            "status": status,
            "evidence": {
                "text": segment.get("raw_text"),
                "start": segment.get("start"),
                "end": segment.get("end"),
            },
        }

    allergy_segment = next(
        (
            segment
            for segment in segments
            if not _is_question_text(segment.get("raw_text"))
            and re.search(
                r"(?:페[네니]슐린|페니실린|penicillin)",
                str(segment.get("raw_text") or ""),
                re.IGNORECASE,
            )
            and re.search(
                r"(?:에너지|알레르|알러지|얼티케리아|두드러기|urticaria)",
                str(segment.get("raw_text") or ""),
                re.IGNORECASE,
            )
        ),
        None,
    )
    if allergy_segment is not None:
        allergy_text = str(allergy_segment.get("raw_text") or "").strip()
        record["allergy"] = grounded_value(
            allergy_segment,
            allergy_text,
            "Penicillin allergy — urticaria",
        )
        past_history["underlying_conditions"] = [
            item
            for item in past_history["underlying_conditions"]
            if not (
                isinstance(item, dict)
                and re.search(
                    r"(?:페[네니]슐린|페니실린|에너지|얼티케리아|urticaria)",
                    str(item.get("raw_value") or ""),
                    re.IGNORECASE,
                )
            )
        ]

    existing_conditions = {
        str(item.get("raw_value") or "").strip(): item
        for item in past_history["underlying_conditions"]
        if isinstance(item, dict)
    }
    for segment in segments:
        segment_text = str(segment.get("raw_text") or "")
        if _is_question_text(segment_text):
            continue
        if re.search(r"(?<![A-Za-z])COPD(?![A-Za-z])", segment_text, re.IGNORECASE):
            if "COPD" not in existing_conditions:
                item = grounded_value(segment, "COPD", "COPD")
                past_history["underlying_conditions"].append(item)
                existing_conditions["COPD"] = item
        hypertension_match = re.search(
            r"하이퍼\s*텐션|hypertension",
            segment_text,
            re.IGNORECASE,
        )
        if hypertension_match:
            raw_hypertension = hypertension_match.group(0)
            existing = existing_conditions.get(raw_hypertension)
            if existing is not None:
                existing["normalized_value"] = "hypertension"
                existing["normalization_provenance"] = {
                    "basis_type": "APPROVED_STT_ALIAS",
                    "status": "APPROVED",
                    "rule_id": "N01_HYPERTENSION_ALIAS",
                }
            else:
                item = grounded_value(
                    segment,
                    raw_hypertension,
                    "hypertension",
                    rule_id="N01_HYPERTENSION_ALIAS",
                    basis_type="APPROVED_STT_ALIAS",
                )
                past_history["underlying_conditions"].append(item)
                existing_conditions[raw_hypertension] = item

    smoking_segment = next(
        (
            segment
            for segment in segments
            if re.search(
                r"(?:\d+\s*)?팩\s*이어|pack\s*-?\s*year|흡연|금연",
                str(segment.get("raw_text") or ""),
                re.IGNORECASE,
            )
            and not _is_question_text(segment.get("raw_text"))
        ),
        None,
    )
    if smoking_segment is not None:
        smoking_text = str(smoking_segment.get("raw_text") or "").strip()
        pack_year = re.search(r"(?P<count>\d+)\s*팩\s*이어", smoking_text)
        quit_year = re.search(r"(?P<years>\d+)\s*년\s*전\s*금연", smoking_text)
        normalized_parts: list[str] = []
        if pack_year:
            normalized_parts.append(f"{pack_year.group('count')} pack-years")
        if quit_year:
            normalized_parts.append(
                f"quit smoking {quit_year.group('years')} years ago"
            )
        if normalized_parts:
            social_history = record.get("social_history")
            if not isinstance(social_history, dict):
                social_history = {}
                record["social_history"] = social_history
            social_history["smoking"] = grounded_value(
                smoking_segment,
                smoking_text,
                "; ".join(normalized_parts),
                rule_id="N02_SMOKING_QUANTITY_AND_CESSATION",
            )

    medications = record.get("medications")
    if not isinstance(medications, dict):
        medications = {}
        record["medications"] = medications
    medications["items"] = _as_list(medications.get("items"))

    def medication_score(text: str) -> int:
        score = 0
        if any(term in text for term in ("먹고", "복용", "드시고", "투약")):
            score += 5
        if any(term in text for term in ("이름", "뭐 먹", "무슨 약", "목록")):
            score += 2
        if "약" in text:
            score += 1
        if "알레르" in text:
            score -= 2
        return score

    answer_segments: list[dict[str, Any]] = []
    for position, segment in enumerate(segments):
        question = str(segment.get("raw_text") or "")
        is_medication_question = _is_question_text(question) and any(
            term in question for term in ("복용약", "드시는 약", "먹는 약", "복용하는 약")
        )
        if not is_medication_question:
            continue
        for answer in segments[position + 1 : position + 3]:
            if _is_question_text(answer.get("raw_text")):
                break
            answer_segments.append(answer)

    best_answer = max(
        answer_segments,
        key=lambda segment: medication_score(str(segment.get("raw_text") or "")),
        default=None,
    )
    existing_score = max(
        (
            medication_score(str(item.get("raw_value") or ""))
            for item in medications["items"]
            if isinstance(item, dict)
        ),
        default=-1,
    )
    if best_answer is not None:
        answer_text = str(best_answer.get("raw_text") or "").strip()
        answer_score = medication_score(answer_text)
        if answer_score > existing_score and answer_score >= 3:
            uncertain = any(
                term in answer_text
                for term in ("모르", "확인", "기억", "집에", "것 같")
            )
            medications["items"] = [
                {
                    "raw_value": answer_text,
                    "status": "needs_confirmation" if uncertain else "confirmed",
                    "evidence": {
                        "text": answer_text,
                        "start": best_answer.get("start"),
                        "end": best_answer.get("end"),
                    },
                }
            ]

    allergy = record.get("allergy")
    has_allergy_value = (
        isinstance(allergy, dict)
        and allergy.get("status") in {"confirmed", "needs_confirmation"}
        and isinstance(allergy.get("raw_value"), str)
        and bool(allergy["raw_value"].strip())
    )
    if not has_allergy_value:
        allergy_answer: dict[str, Any] | None = None
        for position, segment in enumerate(segments):
            question = str(segment.get("raw_text") or "")
            if not (_is_question_text(question) and "알레르" in question):
                continue
            for answer in segments[position + 1 : position + 3]:
                if _is_question_text(answer.get("raw_text")):
                    break
                answer_text = str(answer.get("raw_text") or "").strip()
                if answer_text:
                    allergy_answer = answer
                    break
        if allergy_answer is not None:
            answer_text = str(allergy_answer.get("raw_text") or "").strip()
            uncertain = any(
                term in answer_text for term in ("모르", "기억", "것 같", "확실")
            )
            record["allergy"] = {
                "raw_value": answer_text,
                "status": "needs_confirmation" if uncertain else "confirmed",
                "evidence": {
                    "text": answer_text,
                    "start": allergy_answer.get("start"),
                    "end": allergy_answer.get("end"),
                },
            }

    def display_number(value: Any) -> str:
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    for item in record["physical_examination"]:
        if not isinstance(item, dict):
            continue
        raw_value = str(item.get("raw_value") or "")
        source_segment = evidence_segment(item)
        if source_segment is None:
            continue
        if re.search(r"\bBP\b", raw_value, re.IGNORECASE):
            measurements = [
                candidate
                for annotation in source_segment.get("annotations", [])
                if annotation.get("type") == "numeric_measurement_candidate"
                for candidate in annotation.get("candidates", [])
                if isinstance(candidate, dict)
            ]
            vital_parts: list[str] = []
            for measurement in measurements:
                kind = measurement.get("kind")
                if kind == "blood_pressure":
                    vital_parts.append(
                        f"BP {measurement.get('systolic')}/{measurement.get('diastolic')} mmHg"
                    )
                elif kind == "heart_rate":
                    vital_parts.append(
                        f"HR {display_number(measurement.get('value'))} bpm"
                    )
                elif kind == "respiratory_rate":
                    vital_parts.append(
                        f"RR {display_number(measurement.get('value'))} breaths/min"
                    )
                elif kind == "body_temperature":
                    vital_parts.append(
                        f"BT {display_number(measurement.get('value'))}°C"
                    )
                elif kind == "oxygen_saturation":
                    vital_parts.append(
                        f"SpO₂ {display_number(measurement.get('value'))}%"
                    )
            if vital_parts:
                item["normalized_value"] = "; ".join(vital_parts)
                item["normalization_provenance"] = {
                    "basis_type": "STRUCTURED_MEASUREMENT",
                    "status": "APPROVED",
                    "rule_id": "N03_VITAL_MEASUREMENT_FORMAT",
                }
        if (
            re.search(r"룸(?:웨|에)어", raw_value)
            and re.search(r"위(?:징|증)", raw_value)
            and re.search(r"엑스커터리", raw_value)
            and re.search(r"머슬\s*유세", raw_value)
        ):
            laterality = "bilateral " if "양측" in raw_value else ""
            item["normalized_value"] = (
                f"Room air; {laterality}wheezing; accessory muscle use"
            )
            item["normalization_provenance"] = {
                "basis_type": "APPROVED_STT_ALIAS",
                "status": "APPROVED",
                "rule_id": "N04_RESPIRATORY_EXAM_ALIAS",
            }

    for item in record["impression"]:
        if not isinstance(item, dict):
            continue
        raw_value = str(item.get("raw_value") or "")
        if (
            re.search(r"아큐트", raw_value)
            and re.search(r"엑스어베이", raw_value)
            and re.search(r"COPD", raw_value, re.IGNORECASE)
        ):
            item["normalized_value"] = "Acute exacerbation of COPD"
            item["normalization_provenance"] = {
                "basis_type": "APPROVED_STT_ALIAS",
                "status": "APPROVED",
                "rule_id": "N05_COPD_EXACERBATION_ALIAS",
            }

    for item in record["treatment_plan"]:
        if not isinstance(item, dict):
            continue
        raw_value = str(item.get("raw_value") or "")
        plan_parts: list[str] = []
        if re.search(r"브로콘\s*다일레이터", raw_value):
            plan_parts.append("bronchodilator")
        if re.search(r"옥시전", raw_value):
            plan_parts.append("oxygen requirement assessment")
        if "감염" in raw_value:
            plan_parts.append("infection evaluation")
        if plan_parts:
            item["normalized_value"] = "; ".join(plan_parts)
            item["normalization_provenance"] = {
                "basis_type": "APPROVED_STT_ALIAS",
                "status": "APPROVED",
                "rule_id": "N06_RESPIRATORY_PLAN_ALIAS",
            }

    unresolved: list[Any] = []
    for item in _as_list(normalized.get("unresolved_questions")):
        if not isinstance(item, dict):
            continue
        segment_id = item.get("source_segment_id")
        segment = next(
            (
                source
                for source in segments
                if source.get("id") == segment_id
                or (
                    source.get("start") == item.get("start")
                    and source.get("end") == item.get("end")
                )
            ),
            None,
        )
        if segment is not None and _is_question_text(segment.get("raw_text")):
            unresolved.append(item)
        else:
            warnings.append("non-question unresolved item was discarded")
    normalized["unresolved_questions"] = unresolved
    return normalized


def _empty_draft_fields() -> dict[str, dict[str, Any]]:
    return {
        field_id: {"value": "", "status": "empty", "evidence": []}
        for field_id in DRAFT_FIELD_IDS
    }


def _atomic_values(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _atomic_values(item)
        return
    if not isinstance(value, dict):
        return
    if "status" in value:
        yield value
        return
    for item in value.values():
        yield from _atomic_values(item)


def _deduplicated(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _evidence_with_segment_id(
    evidence: Any,
    api3_segments: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(evidence, dict):
        return None
    source_segment_id = evidence.get("source_segment_id")
    start = evidence.get("start")
    end = evidence.get("end")
    text = evidence.get("text")
    if source_segment_id is not None:
        match = next(
            (
                segment
                for segment in api3_segments
                if segment.get("id") == source_segment_id
            ),
            None,
        )
        if match is not None:
            return {
                "text": text or match.get("raw_text"),
                "start": start if start is not None else match.get("start"),
                "end": end if end is not None else match.get("end"),
                "segment_id": match.get("id"),
                "raw_text": match.get("raw_text"),
            }
    match = next(
        (
            segment
            for segment in api3_segments
            if segment.get("start") == start
            and segment.get("end") == end
            and text in {segment.get("raw_text"), segment.get("corrected_text")}
        ),
        None,
    )
    if match is None:
        match = next(
            (
                segment
                for segment in api3_segments
                if segment.get("start") == start and segment.get("end") == end
            ),
            None,
        )
    result = {"text": text, "start": start, "end": end}
    if match is not None:
        result["segment_id"] = match.get("id")
        result["raw_text"] = match.get("raw_text")
    return result


def _english_draft_value(
    item: dict[str, Any],
    api3_segments: list[dict[str, Any]],
    candidate_decisions: list[dict[str, Any]],
) -> str:
    raw_value = str(item.get("raw_value") or "").strip()
    if not raw_value:
        return ""
    normalized_value = item.get("normalized_value")
    provenance = item.get("normalization_provenance")
    approved_basis = (
        str(provenance.get("basis_type") or "")
        if isinstance(provenance, dict)
        and provenance.get("status") == "APPROVED"
        else ""
    )
    if (
        isinstance(normalized_value, str)
        and normalized_value.strip()
        and approved_basis
        in {"APPROVED_RULE", "APPROVED_STT_ALIAS", "STRUCTURED_MEASUREMENT", "CLINICIAN_EDIT"}
    ):
        return normalized_value.strip()
    evidence = _evidence_with_segment_id(item.get("evidence"), api3_segments)
    segment_id = evidence.get("segment_id") if evidence else None
    segment = next(
        (value for value in api3_segments if value.get("id") == segment_id),
        None,
    )
    if segment is None:
        return raw_value
    decisions = {
        (decision.get("segment_id"), decision.get("annotation_index")): decision
        for decision in candidate_decisions
        if isinstance(decision, dict)
    }
    display_value = raw_value
    replacements: list[tuple[str, str]] = []
    for annotation_index, annotation in enumerate(segment.get("annotations", [])):
        if annotation.get("type") not in {
            "medical_term_candidate",
            "diagnosis_term_candidate",
        }:
            continue
        if _requires_clinician_candidate_review(annotation):
            continue
        source_text = str((annotation.get("source_span") or {}).get("text") or "")
        if not source_text or source_text not in display_value:
            continue
        candidates: list[dict[str, Any]] = []
        if annotation.get("needs_review"):
            decision = decisions.get((segment_id, annotation_index), {})
            if decision.get("action") == "selected":
                candidates = [
                    candidate
                    for candidate in decision.get("selected_candidates", [])
                    if isinstance(candidate, dict)
                ]
        else:
            candidates = [
                candidate
                for candidate in annotation.get("candidates", [])[:1]
                if isinstance(candidate, dict)
            ]
        english_terms = _deduplicated(
            str(candidate.get("canonical_en") or "").strip()
            for candidate in candidates
        )
        if english_terms:
            replacement = (
                source_text
                if re.fullmatch(r"[A-Z][A-Z0-9₂-]{1,}", source_text)
                else " / ".join(english_terms)
            )
            replacements.append((source_text, replacement))
    for source_text, replacement in sorted(
        replacements, key=lambda value: len(value[0]), reverse=True
    ):
        particle_pattern = re.compile(
            rf"{re.escape(source_text)}(?:가|이|은|는|을|를|와|과|도)(?=\s|[,.;!?]|$)"
        )
        display_value = particle_pattern.sub(replacement, display_value)
        display_value = display_value.replace(source_text, replacement)
    return display_value


def _requires_clinician_candidate_review(annotation: dict[str, Any]) -> bool:
    return any(
        isinstance(candidate, dict)
        and str(candidate.get("match_type") or "").casefold()
        in _CLINICIAN_REVIEW_ONLY_MATCH_TYPES
        for candidate in annotation.get("candidates", [])
    )


def _draft_field(
    source: Any,
    api3_segments: list[dict[str, Any]],
    *,
    empty_when_missing: bool = False,
    candidate_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    atoms = list(_atomic_values(source))
    values = _deduplicated(
        _english_draft_value(
            item,
            api3_segments,
            candidate_decisions or [],
        )
        for item in atoms
        if item.get("raw_value") not in (None, "")
    )
    statuses = {str(item.get("status")) for item in atoms}
    evidence: list[dict[str, Any]] = []
    for item in atoms:
        normalized = _evidence_with_segment_id(item.get("evidence"), api3_segments)
        if normalized is not None and normalized not in evidence:
            evidence.append(normalized)

    if values:
        status = (
            "needs_review"
            if statuses & {"needs_confirmation", "asked_but_unanswered"}
            else "filled"
        )
        value = "\n".join(values)
    elif statuses & {"needs_confirmation", "asked_but_unanswered"}:
        status = "needs_review"
        value = "확인 필요"
    elif empty_when_missing:
        status = "empty"
        value = ""
    else:
        status = "unknown"
        value = "미확인"
    return {"value": value, "status": status, "evidence": evidence}


def _chief_draft_field(
    source: Any,
    api3_segments: list[dict[str, Any]],
    *,
    candidate_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    atoms = list(_atomic_values(source))
    segments = {
        str(segment.get("id")): segment
        for segment in api3_segments
        if isinstance(segment, dict) and segment.get("id") is not None
    }
    values: list[str] = []
    seen_values: set[str] = set()
    evidence_values: list[dict[str, Any]] = []
    allowed_term_types = FIELD_POLICIES["chief_complaint"].allowed_term_types

    def append_value(value: Any) -> None:
        normalized = " ".join(str(value or "").split()).strip(" ;")
        identity = normalized.casefold()
        if normalized and identity not in seen_values:
            seen_values.add(identity)
            values.append(normalized)

    for atom in atoms:
        raw_value = str(atom.get("raw_value") or "").strip()
        if not raw_value:
            continue
        evidence = _evidence_with_segment_id(atom.get("evidence"), api3_segments)
        if evidence is not None and evidence not in evidence_values:
            evidence_values.append(evidence)
        segment = segments.get(str(evidence.get("segment_id"))) if evidence else None
        atom_values: list[str] = []
        if segment is not None:
            for annotation in segment.get("annotations", []):
                if not isinstance(annotation, dict):
                    continue
                term_type = annotation.get("term_type")
                source_text = str(
                    (annotation.get("source_span") or {}).get("text") or ""
                ).strip()
                if source_text and not (
                    source_text.casefold() in raw_value.casefold()
                    or raw_value.casefold() in source_text.casefold()
                ):
                    continue
                candidates = filter_candidates_for_field(
                    "chief_complaint",
                    annotation.get("candidates", []),
                    annotation_term_type=term_type,
                )
                if term_type not in allowed_term_types and not candidates:
                    continue
                translated_term = next(
                    (
                        str(term).strip()
                        for term in annotation.get("search_terms_en", [])
                        if str(term).strip()
                    ),
                    "",
                )
                if translated_term:
                    atom_values.append(
                        translated_term[:1].upper() + translated_term[1:]
                    )
                    continue
                canonical_en = next(
                    (
                        str(candidate.get("canonical_en") or "").strip()
                        for candidate in candidates
                        if str(candidate.get("canonical_en") or "").strip()
                    ),
                    "",
                )
                if canonical_en:
                    atom_values.append(canonical_en)
            if not atom_values:
                translated_text = str(segment.get("translated_text_en") or "").strip()
                if translated_text:
                    atom_values.append(translated_text)
        if not atom_values:
            atom_values.append(
                _english_draft_value(
                    atom,
                    api3_segments,
                    candidate_decisions or [],
                )
            )
        for value in atom_values:
            append_value(value)

    statuses = {str(item.get("status")) for item in atoms}
    if values:
        status = (
            "needs_review"
            if statuses & {"needs_confirmation", "asked_but_unanswered"}
            else "filled"
        )
        value = ", ".join(values)
    elif statuses & {"needs_confirmation", "asked_but_unanswered"}:
        status = "needs_review"
        value = "확인 필요"
    else:
        status = "unknown"
        value = "미확인"
    return {"value": value, "status": status, "evidence": evidence_values}


def _ros_symptom_atoms(record: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield from _atomic_values(record.get("chief_complaint"))
    history = record.get("history_of_present_illness")
    if isinstance(history, dict):
        yield from _atomic_values(history.get("associated_symptoms"))
    yield from _atomic_values(record.get("review_of_systems"))


def _ros_candidate_labels(
    atom: dict[str, Any],
    segment: dict[str, Any],
) -> tuple[list[str], bool]:
    raw_value = str(atom.get("raw_value") or "").strip()
    labels: list[str] = []
    needs_review = False
    for annotation in segment.get("annotations", []):
        if (
            not isinstance(annotation, dict)
            or annotation.get("type") != "medical_term_candidate"
        ):
            continue
        source_text = str(
            (annotation.get("source_span") or {}).get("text") or ""
        ).strip()
        if source_text and source_text not in raw_value and raw_value not in source_text:
            continue
        candidates = [
            candidate
            for candidate in annotation.get("candidates", [])
            if isinstance(candidate, dict)
            and SYMPTOM
            in candidate_term_types(
                candidate,
                annotation_term_type=annotation.get("term_type"),
            )
        ]
        if not candidates:
            continue
        candidate = candidates[0]
        label = str(
            candidate.get("canonical_ko")
            or candidate.get("canonical_en")
            or ""
        ).strip()
        if label and label not in labels:
            labels.append(label)
        needs_review = needs_review or bool(annotation.get("needs_review"))
    return labels, needs_review


_ROS_NEGATION_RE = re.compile(
    r"(?:없(?:습니다|어요|다|음|었)|아니(?:요|다|었습니다)|"
    r"하지\s*않|안\s+\S+|부인)"
)
_ROS_UNCERTAINTY_RE = re.compile(
    r"(?:모르|기억(?:이)?\s*(?:안|못)|확실하지|불확실|정확하지|"
    r"것\s*같|듯(?:해|합니|하|$)|아마|확인\s*필요)"
)


def _ros_assertion(atom: dict[str, Any]) -> str:
    raw_value = str(atom.get("raw_value") or "")
    status = str(atom.get("status") or "")
    if status in {"needs_confirmation", "asked_but_unanswered"} or (
        _ROS_UNCERTAINTY_RE.search(raw_value)
    ):
        return "uncertain"
    if _ROS_NEGATION_RE.search(raw_value):
        return "negative"
    return "positive"


def _ros_summary_field(
    record: dict[str, Any],
    api3_segments: list[dict[str, Any]],
) -> dict[str, Any]:
    segments = {
        str(segment.get("id")): segment
        for segment in api3_segments
        if isinstance(segment, dict) and segment.get("id") is not None
    }
    assertions_by_label: dict[str, tuple[str, set[str]]] = {}
    evidence_values: list[dict[str, Any]] = []
    needs_review = False
    for atom in _ros_symptom_atoms(record):
        evidence = _evidence_with_segment_id(atom.get("evidence"), api3_segments)
        segment = segments.get(str(evidence.get("segment_id"))) if evidence else None
        if segment is None:
            continue
        labels, candidate_needs_review = _ros_candidate_labels(atom, segment)
        if not labels:
            raw_label = str(atom.get("raw_value") or "").strip(" \t\r\n.,!?")
            labels = [raw_label] if raw_label else []
        assertion = _ros_assertion(atom)
        for label in labels:
            identity = " ".join(label.split()).casefold()
            if identity not in assertions_by_label:
                assertions_by_label[identity] = (label, set())
            assertions_by_label[identity][1].add(assertion)
        if labels and evidence not in evidence_values:
            evidence_values.append(evidence)
        needs_review = (
            needs_review
            or candidate_needs_review
            or assertion == "uncertain"
        )
    if not assertions_by_label:
        return {"value": "", "status": "empty", "evidence": []}
    values: list[str] = []
    for label, assertions in assertions_by_label.values():
        if "positive" in assertions and "negative" in assertions:
            suffix = "+/- · 확인 필요"
            needs_review = True
        elif "uncertain" in assertions:
            suffix = "확인 필요"
        elif "negative" in assertions:
            suffix = "-"
        else:
            suffix = "+"
        values.append(f"{label}({suffix})")
    return {
        "value": "\n".join(values),
        "status": "needs_review" if needs_review else "filled",
        "evidence": evidence_values,
    }


def _nrs_draft_field(
    pain_assessment: Any,
    api3_segments: list[dict[str, Any]],
) -> dict[str, Any]:
    nrs = pain_assessment.get("nrs") if isinstance(pain_assessment, dict) else None
    field = _draft_field(nrs, api3_segments)
    if not field["value"] or field["value"] in {"미확인", "확인 필요"}:
        return field
    match = re.search(r"(?<!\d)(10(?:\.0+)?|[0-9](?:\.\d+)?)(?!\d)", field["value"])
    if match:
        score = match.group(1).removesuffix(".0")
        field["value"] = f"NRS {score}/10"
    else:
        field["status"] = "needs_review"
        field["value"] = f"NRS 확인 필요: {field['value']}"
    return field


def _candidate_field(
    segment: dict[str, Any],
    previous_segment: dict[str, Any] | None,
    candidates: list[dict[str, Any]],
) -> str:
    text = str(segment.get("raw_text") or "")
    collections = {candidate.get("collection") for candidate in candidates}
    candidate_terms = [
        str(
            candidate.get("canonical_en")
            or candidate.get("canonical_ko")
            or candidate.get("code_display")
            or candidate.get("retrieved_text")
            or ""
        ).strip()
        for candidate in candidates
    ]
    candidate_text = " ".join(
        candidate_term for candidate_term in candidate_terms if candidate_term
    )
    context_text = " ".join(
        (
            str(previous_segment.get("raw_text") or "")
            if previous_segment is not None
            else "",
            text,
        )
    )
    lowered_terms = [term.casefold() for term in candidate_terms]
    if any(
        term == "hypertension"
        or term.endswith(" hypertension")
        or re.search(r"(?<!항)고혈압(?!제|약)", term)
        for term in lowered_terms
    ):
        return "past-history"
    if any("urticaria" in term or "두드러기" in term for term in lowered_terms):
        return "allergy"
    if any(
        "wheezing" in term or "천명" in term or "쌕쌕" in term
        for term in lowered_terms
    ):
        return "physical"
    if any(
        "bronchodilator" in term or "기관지확장제" in term
        for term in lowered_terms
    ):
        return "treatment-plan"
    respiratory_symptom = any(
        re.search(r"\b(?:cough|sputum|dyspnea)\b", term)
        or any(korean in term for korean in ("기침", "가래", "객담", "호흡곤란", "숨참"))
        for term in lowered_terms
    )
    if respiratory_symptom:
        previous_text = (
            str(previous_segment.get("raw_text") or "")
            if previous_segment is not None
            else ""
        )
        if _is_question_text(previous_text) and re.search(
            r"(?:어디가|불편해서|왜\s*오|무슨\s*일|가장\s*불편|주된\s*증상|어떤\s*증상)",
            previous_text,
        ):
            return "chief"
        return "history"
    medication_context = any(
        term in context_text
        for term in ("약", "먹고", "복용", "드시", "투약", "처방")
    )
    medication_concept = bool(
        re.search(
            r"항응고제|항고혈압제|고혈압약|항구토제|항생제|해열제|진통제|"
            r"소염제|인슐린|복용약|약물",
            candidate_text,
        )
    )
    if "drug_terms" in collections or (medication_context and medication_concept):
        return "medication"
    if any(
        term in text
        for term in ("과거", "예전", "이전", "진단받", "진단을 받", "앓았", "입원했")
    ):
        return "past-history"
    if any(term in text for term in ("진단", "의심", "의증", "소견", "으로 보")):
        return "impression"
    if "procedure_terms" in collections:
        if any(term in text for term in ("했", "받았", "과거", "이전")):
            return "past-history"
        return "treatment-plan"
    if "kcd9_terms" in collections:
        if any(term in text for term in ("진단", "의심", "소견", "으로 보")):
            return "impression"
        if any(term in text for term in ("과거", "예전", "진단받", "앓고")):
            return "past-history"
        if previous_segment and _is_question_text(previous_segment.get("raw_text")):
            return "review-of-systems"
        return "history"
    if "emergency_terms" in collections and previous_segment and _is_question_text(
        previous_segment.get("raw_text")
    ):
        return "review-of-systems"
    return "history"


def _review_items(
    api3_document: dict[str, Any],
    candidate_decisions: list[dict[str, Any]] | None = None,
    clinical_record: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    evidence_routes = evidence_fields_by_segment(
        clinical_record or {},
        segments=api3_document.get("segments") or [],
    )
    decision_actions = {
        (decision.get("segment_id"), decision.get("annotation_index")): decision.get(
            "action"
        )
        for decision in candidate_decisions or []
        if isinstance(decision, dict)
    }
    segments = list(api3_document.get("segments", []))
    for segment_position, segment in enumerate(segments):
        if _is_question_text(segment.get("raw_text")):
            continue
        for annotation_index, annotation in enumerate(segment.get("annotations", [])):
            if not annotation.get("needs_review") or annotation.get("type") not in {
                "medical_term_candidate",
                "diagnosis_term_candidate",
                "unresolved_medical_term",
            }:
                continue
            if candidate_decisions is not None and decision_actions.get(
                (segment.get("id"), annotation_index),
                "needs_review",
            ) != "needs_review":
                continue
            raw_candidates = [
                candidate
                for candidate in annotation.get("candidates", [])
                if isinstance(candidate, dict)
            ]
            source_span = annotation.get("source_span") or {}
            term_type = annotation.get("term_type")
            grounded_evidence = evidence_routes.get(str(segment.get("id")), ())
            canonical_field = choose_evidence_field(
                grounded_evidence,
                source_text=str(source_span.get("text") or ""),
                candidates=raw_candidates,
                annotation_term_type=term_type,
            )
            if canonical_field is None:
                # A candidate that conflicts with a field already grounded to
                # this segment must not be moved to another draft field. Keep
                # the conversation-grounded draft and omit the unrelated hit.
                if grounded_evidence:
                    continue
                canonical_field = fallback_field_for_term_type(term_type)
            previous_segment = (
                segments[segment_position - 1] if segment_position > 0 else None
            )
            field_id = (
                CANONICAL_TO_DRAFT_FIELD.get(canonical_field)
                if canonical_field is not None
                else None
            ) or _candidate_field(segment, previous_segment, raw_candidates)
            candidates = filter_candidates_for_field(
                field_id,
                raw_candidates,
                annotation_term_type=term_type,
            )
            names = _deduplicated(
                str(
                    candidate.get("canonical_en")
                    or candidate.get("code_display")
                    or candidate.get("code")
                    or candidate.get("canonical_ko")
                    or ""
                ).strip()
                for candidate in candidates
            )
            search_terms_en = [
                str(term).strip()
                for term in annotation.get("search_terms_en", [])
                if str(term).strip()
            ]
            if (
                not names
                and not raw_candidates
                and annotation.get("type") != "unresolved_medical_term"
            ):
                continue
            explicit_drug_type = _explicit_drug_entity_type(annotation)
            candidate_details: list[dict[str, Any]] = []
            candidate_provenance: list[dict[str, Any]] = []
            seen_candidate_details: set[tuple[Any, Any, str]] = set()
            for candidate in candidates:
                match_type = str(candidate.get("match_type") or "").casefold()
                display_value = str(
                    candidate.get("canonical_en")
                    or candidate.get("code_display")
                    or candidate.get("code")
                    or candidate.get("canonical_ko")
                    or ""
                ).strip()
                declared_source = str(
                    (candidate.get("provenance") or {}).get("source") or ""
                ).upper()
                if declared_source not in {"RAW_EXACT", "UMLS", "NGRAM_FALLBACK"}:
                    if match_type == "umls_dictionary_search":
                        declared_source = "UMLS"
                    elif match_type == "ngram_dictionary_fallback":
                        declared_source = "NGRAM_FALLBACK"
                    else:
                        declared_source = "RAW_EXACT"
                provenance = candidate.get("provenance")
                provenance = provenance if isinstance(provenance, dict) else {}
                if display_value:
                    candidate_provenance.append(
                        {
                            "display_value": display_value,
                            "source": declared_source,
                            "cui": provenance.get("cui"),
                            "semantic_types": list(provenance.get("semantic_types") or []),
                            "similarity": provenance.get("similarity"),
                        }
                    )
                if match_type in {
                    "umls_dictionary_search",
                    "ngram_dictionary_fallback",
                }:
                    # Translated evidence has no independently verified RAW alias
                    # span. Keep it selectable for this draft, but never send the
                    # whole RAW segment to the cumulative alias database.
                    continue
                identity = (
                    candidate.get("collection"),
                    candidate.get("entity_id"),
                    display_value,
                )
                if not display_value or identity in seen_candidate_details:
                    continue
                seen_candidate_details.add(identity)
                entity_type = candidate.get("entity_type")
                candidate_details.append(
                    {
                        "display_value": display_value,
                        "collection": candidate.get("collection"),
                        "entity_id": candidate.get("entity_id"),
                        "canonical_ko": candidate.get("canonical_ko"),
                        "canonical_en": candidate.get("canonical_en"),
                        "entity_type": entity_type,
                        "source_entity_type": (
                            explicit_drug_type
                            if candidate.get("collection") == "drug_terms"
                            else entity_type
                        ),
                    }
                )
            assertion = _candidate_assertion(segment, dict(source_span))
            items.append(
                {
                    "id": f"{segment.get('id')}:{annotation_index}",
                    "type": annotation.get("type"),
                    "field_id": field_id,
                    "segment_id": segment.get("id"),
                    "source": source_span.get("text", ""),
                    "evidence": segment.get("raw_text", ""),
                    "evidence_start": segment.get("start"),
                    "evidence_end": segment.get("end"),
                    "candidates": names,
                    "candidate_details": candidate_details,
                    "candidate_provenance": candidate_provenance,
                    "search_terms_en": search_terms_en,
                    "term_type": term_type,
                    "assertion": assertion,
                    "needs_review": True,
                }
            )
    return items


def _apply_draft_suggestions(
    fields: dict[str, dict[str, Any]],
    suggestions: Any,
) -> None:
    for field in fields.values():
        original = str(field.get("value") or "")
        field["ai_original_value"] = original
        field["suggestion_status"] = "UNCHANGED"
        field["applied_candidates"] = []
    if not isinstance(suggestions, list):
        return

    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        legacy_field_id = _CANONICAL_TO_LEGACY_FIELD_ID.get(
            str(suggestion.get("field_id") or "")
        )
        field = fields.get(legacy_field_id) if legacy_field_id else None
        if field is None:
            continue
        original_value = str(suggestion.get("original_value") or "").strip()
        suggested_value = str(suggestion.get("suggested_value") or "").strip()
        current_value = str(field.get("value") or "")
        if (
            not original_value
            or not suggested_value
            or original_value == suggested_value
            or original_value not in current_value
        ):
            continue
        applied_candidates: list[dict[str, str]] = []
        for candidate in suggestion.get("applied_candidates", []):
            if not isinstance(candidate, dict):
                continue
            projected = {
                "collection": str(candidate.get("collection") or "").strip(),
                "entity_id": str(candidate.get("entity_id") or "").strip(),
                "display_value": str(candidate.get("display_value") or "").strip(),
                "source": str(candidate.get("source") or "").strip(),
            }
            if (
                all(projected.values())
                and projected["source"] in {"RAW_EXACT", "UMLS"}
                and projected["display_value"] in suggested_value
            ):
                applied_candidates.append(projected)
        if not applied_candidates:
            continue
        field["value"] = current_value.replace(original_value, suggested_value, 1)
        field["suggestion_status"] = "AUTO_SUGGESTED"
        existing = {
            (candidate["collection"], candidate["entity_id"])
            for candidate in field["applied_candidates"]
        }
        for candidate in applied_candidates:
            identity = (candidate["collection"], candidate["entity_id"])
            if identity not in existing:
                field["applied_candidates"].append(candidate)
                existing.add(identity)


def _explicit_drug_entity_type(annotation: dict[str, Any]) -> str | None:
    exact_types: set[str] = set()
    for candidate in annotation.get("candidates", []):
        if (
            not isinstance(candidate, dict)
            or candidate.get("collection") != "drug_terms"
        ):
            continue
        entity_type = str(candidate.get("entity_type") or "").casefold()
        if entity_type not in {"ingredient", "product"}:
            continue
        match_type = str(candidate.get("match_type") or "").casefold()
        string_similarity = candidate.get("string_similarity")
        edit_similarity = candidate.get("edit_similarity")
        exact_similarity = (
            isinstance(string_similarity, (int, float))
            and not isinstance(string_similarity, bool)
            and isinstance(edit_similarity, (int, float))
            and not isinstance(edit_similarity, bool)
            and float(string_similarity) >= 0.999999
            and float(edit_similarity) >= 0.999999
        )
        if (
            match_type in {"alias_exact", "official_exact", "stt_alias_exact"}
            or exact_similarity
        ):
            exact_types.add(entity_type)
    return next(iter(exact_types)) if len(exact_types) == 1 else None


def _candidate_decisions(
    api2_document: dict[str, Any] | None,
    api3_document: dict[str, Any],
) -> list[dict[str, Any]]:
    model_decisions = (
        api2_document.get("candidate_decisions")
        if isinstance(api2_document, dict)
        else []
    )
    if not isinstance(model_decisions, list):
        model_decisions = []

    segments = {
        segment.get("id"): segment for segment in api3_document.get("segments", [])
    }
    grounded: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for model_decision in model_decisions:
        if not isinstance(model_decision, dict):
            continue
        segment_id = model_decision.get("segment_id")
        annotation_index = model_decision.get("annotation_index")
        key = (segment_id, annotation_index)
        if key in seen or not isinstance(annotation_index, int):
            continue
        segment = segments.get(segment_id)
        annotations = segment.get("annotations", []) if segment else []
        if not 0 <= annotation_index < len(annotations):
            continue
        annotation = annotations[annotation_index]
        if annotation.get("type") not in {
            "medical_term_candidate",
            "diagnosis_term_candidate",
        } or not annotation.get("needs_review"):
            continue

        candidates = annotation.get("candidates", [])
        candidates_by_id = {
            candidate.get("entity_id"): candidate
            for candidate in candidates
            if candidate.get("entity_id") is not None
        }
        selected_ids = model_decision.get("selected_candidate_ids")
        selected_ids = selected_ids if isinstance(selected_ids, list) else []
        selected = [
            candidates_by_id[candidate_id]
            for candidate_id in selected_ids
            if candidate_id in candidates_by_id
        ][:2]
        action = model_decision.get("action")
        if action not in {"selected", "rejected_all", "needs_review"}:
            action = "needs_review"
        if action == "selected" and not selected:
            action = "needs_review"
        if action == "rejected_all":
            selected = []

        confidence = model_decision.get("confidence")
        reason = model_decision.get("reason")
        explicit_drug_type = _explicit_drug_entity_type(annotation)
        if action == "selected" and explicit_drug_type:
            type_preserving = [
                candidate
                for candidate in selected
                if str(candidate.get("entity_type") or "").casefold()
                == explicit_drug_type
            ]
            if type_preserving:
                selected = type_preserving
            elif selected:
                action = "needs_review"
                selected = []
                confidence = None
                reason = (
                    "Selected drug candidate changes the explicitly matched "
                    f"{explicit_drug_type} naming level"
                )
        assertion = _candidate_assertion(
            segment,
            dict(annotation.get("source_span") or {}),
        )
        if assertion == "question_only":
            action = "rejected_all"
            selected = []
            confidence = None
            reason = "Question-only candidate cannot establish a clinical fact"
        elif assertion == "uncertain" and action == "selected":
            action = "needs_review"
            confidence = None
            reason = "Candidate meaning remains uncertain in its answer span"
        if (
            assertion != "question_only"
            and _requires_clinician_candidate_review(annotation)
        ):
            action = "needs_review"
            selected = []
            confidence = None
            reason = (
                "Resolver-generated candidate requires explicit clinician review"
            )

        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            confidence = None
        if not isinstance(reason, str):
            reason = ""
        grounded.append(
            {
                "segment_id": segment_id,
                "annotation_index": annotation_index,
                "source_span": dict(annotation.get("source_span") or {}),
                "action": action,
                "selected_candidates": selected,
                "confidence": confidence,
                "needs_review": action == "needs_review",
                "reason": reason,
            }
        )
        seen.add(key)

    for segment in api3_document.get("segments", []):
        for annotation_index, annotation in enumerate(segment.get("annotations", [])):
            if annotation.get("type") not in {
                "medical_term_candidate",
                "diagnosis_term_candidate",
            } or not annotation.get("needs_review"):
                continue
            key = (segment.get("id"), annotation_index)
            if key in seen:
                continue
            question_only = (
                _candidate_assertion(
                    segment,
                    dict(annotation.get("source_span") or {}),
                )
                == "question_only"
            )
            grounded.append(
                {
                    "segment_id": segment.get("id"),
                    "annotation_index": annotation_index,
                    "source_span": dict(annotation.get("source_span") or {}),
                    "action": "rejected_all" if question_only else "needs_review",
                    "selected_candidates": [],
                    "confidence": None,
                    "needs_review": not question_only,
                    "reason": (
                        "Question-only candidate cannot establish a clinical fact"
                        if question_only
                        else "Gemma returned no grounded decision"
                    ),
                }
            )
    return grounded


def _api2_candidate_decisions(
    grounded_decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "segment_id": decision["segment_id"],
            "annotation_index": decision["annotation_index"],
            "action": decision["action"],
            "selected_candidate_ids": [
                candidate["entity_id"]
                for candidate in decision["selected_candidates"]
                if candidate.get("entity_id") is not None
            ],
            "confidence": decision["confidence"],
            "reason": decision["reason"],
        }
        for decision in grounded_decisions
    ]


def build_draft(
    api2_document: dict[str, Any] | None,
    api3_document: dict[str, Any],
    candidate_decisions: list[dict[str, Any]] | None = None,
    translated_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    fields = _empty_draft_fields()
    translations = {
        str(item.get("segment_id")): str(item.get("translated_text_en") or "").strip()
        for item in translated_segments or []
        if isinstance(item, dict)
        and item.get("segment_id") is not None
        and str(item.get("translated_text_en") or "").strip()
    }
    api3_segments = [
        {
            **segment,
            **(
                {"translated_text_en": translations[str(segment.get("id"))]}
                if str(segment.get("id")) in translations
                else {}
            ),
        }
        for segment in api3_document.get("segments", [])
        if isinstance(segment, dict)
    ]
    record: dict[str, Any] = {}
    if api2_document is not None:
        record = api2_document.get("clinical_record") or {}
        for field_id, source_key in _SUPPORTED_FIELD_SOURCES.items():
            if field_id == "pain":
                fields[field_id] = _nrs_draft_field(
                    record.get(source_key), api3_segments
                )
            elif field_id == "chief":
                fields[field_id] = _chief_draft_field(
                    record.get(source_key),
                    api3_segments,
                    candidate_decisions=candidate_decisions,
                )
            else:
                fields[field_id] = _draft_field(
                    record.get(source_key),
                    api3_segments,
                    candidate_decisions=candidate_decisions,
                    empty_when_missing=field_id
                    in {
                        "social",
                        "review-of-systems",
                        "physical",
                        "impression",
                        "treatment-plan",
                    },
                )

        fields["review-of-systems"] = _ros_summary_field(record, api3_segments)

        _apply_draft_suggestions(
            fields,
            api2_document.get("draft_suggestions"),
        )
    else:
        _apply_draft_suggestions(fields, None)

    review_items = _review_items(
        api3_document,
        candidate_decisions,
        record,
    )
    for item in review_items:
        field = fields[item["field_id"]]
        field["status"] = "needs_review"
        if not item.get("candidates") and field["suggestion_status"] == "UNCHANGED":
            field["suggestion_status"] = "UNRESOLVED"
    for field in fields.values():
        field["value"] = _clinical_note_style(field.get("value"))
    return {"fields": fields, "review_items": review_items}


def _api2_payload(
    whisper_payload: dict[str, Any],
    api3_document: dict[str, Any],
    query_expansion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_keys = {
        "collection",
        "entity_id",
        "canonical_ko",
        "canonical_en",
        "entity_type",
        "code",
        "code_display",
        "match_type",
        "retrieval_score",
        "retrieved_text",
        "provenance",
    }

    def compact_annotations(annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compacted: list[dict[str, Any]] = []
        for annotation_index, annotation in enumerate(annotations):
            if not annotation.get("needs_review"):
                continue
            item = {
                "annotation_index": annotation_index,
                "type": annotation["type"],
                "source_span": annotation["source_span"],
                "candidates": [
                    {
                        key: value
                        for key, value in candidate.items()
                        if key in candidate_keys
                    }
                    for candidate in annotation.get("candidates", [])
                ],
                "needs_review": annotation["needs_review"],
            }
            if annotation.get("search_terms_en"):
                item["search_terms_en"] = list(annotation["search_terms_en"])
            if annotation.get("term_type"):
                item["term_type"] = annotation["term_type"]
            compacted.append(item)
        return compacted

    translations = {
        item.get("segment_id"): item.get("translated_text_en")
        for item in (
            query_expansion.get("translated_segments", [])
            if isinstance(query_expansion, dict)
            else []
        )
        if isinstance(item, dict)
        and isinstance(item.get("translated_text_en"), str)
        and item.get("translated_text_en", "").strip()
    }
    result = dict(whisper_payload)
    result["segments"] = [
        {
            "id": segment["id"],
            "start": segment["start"],
            "end": segment["end"],
            "text": segment["corrected_text"],
            "raw_text": segment["raw_text"],
            "corrected_text": segment["corrected_text"],
            **(
                {"translated_text_en": translations[segment["id"]]}
                if segment["id"] in translations
                else {}
            ),
            "annotations": compact_annotations(segment["annotations"]),
        }
        for segment in api3_document["segments"]
    ]
    return result


def _workflow_audit(
    api3_document: dict[str, Any],
    api2_document: dict[str, Any] | None,
) -> dict[str, Any]:
    api3_metadata = (
        api3_document.get("metadata")
        if isinstance(api3_document.get("metadata"), dict)
        else {}
    )
    api2_metadata = (
        api2_document.get("metadata")
        if isinstance(api2_document, dict)
        and isinstance(api2_document.get("metadata"), dict)
        else {}
    )
    return {
        "schema_version": "clinical-workflow-audit-v1",
        "references": {
            "query_expansion_path": "$.query_expansion",
            "segments_path": "$.api3.segments",
            "clinical_record_path": "$.api2.clinical_record",
            "candidate_decisions_path": "$.candidate_decisions",
            "errors_path": "$.errors",
        },
        "versions": {
            "workflow_schema": SCHEMA_VERSION,
            "api3_schema": api3_document.get("schema_version"),
            "clinical_record_schema": (
                api2_document.get("schema_version")
                if isinstance(api2_document, dict)
                else None
            ),
            "model": api2_metadata.get("model"),
            "clinical_prompt": api2_metadata.get("prompt_version"),
            "candidate_prompt": api2_metadata.get("candidate_prompt_version"),
            "draft_normalization_prompt": api2_metadata.get(
                "draft_normalization_prompt_version"
            ),
            "alias_db": api3_metadata.get("alias_db_version"),
        },
        "timestamps": {
            "api3_created_at": api3_metadata.get("created_at"),
            "clinical_record_created_at": api2_metadata.get("created_at"),
        },
    }


def _resolved_candidates_for_pipeline(
    validated_segments: list[dict[str, Any]],
    query_expansion: dict[str, Any],
    medical_query_resolver: Any,
    *,
    collection_hints_by_source_id: dict[Any, frozenset[str]] | None = None,
    medical_term_query_source_ids: frozenset[str] = frozenset(),
) -> tuple[dict[Any, list[dict[str, Any]]], Any]:
    from .medical_query_resolver import (
        MedicalQueryDocument,
        MedicalQuerySegment,
    )

    translations: dict[str | int, str] = {}
    translated_segments = query_expansion.get("translated_segments")
    if isinstance(translated_segments, list):
        for translated in translated_segments:
            if not isinstance(translated, dict):
                continue
            source_id = translated.get("segment_id")
            translated_text = translated.get("translated_text_en")
            if (
                isinstance(source_id, (str, int))
                and not isinstance(source_id, bool)
                and isinstance(translated_text, str)
                and translated_text.strip()
            ):
                translations.setdefault(source_id, translated_text)

    raw_text_by_source_id = {
        source["id"]: source["text"] for source in validated_segments
    }
    expansion_spans: dict[str | int, list[dict[str, Any]]] = {}
    for item in query_expansion.get("items") or []:
        if not isinstance(item, dict):
            continue
        source_id = item.get("segment_id")
        raw_text = raw_text_by_source_id.get(source_id)
        source_span = item.get("source_span")
        if not isinstance(raw_text, str) or not isinstance(source_span, dict):
            continue
        source_text = source_span.get("text")
        start_char = source_span.get("start_char")
        end_char = source_span.get("end_char")
        if (
            not isinstance(source_text, str)
            or not isinstance(start_char, int)
            or isinstance(start_char, bool)
            or not isinstance(end_char, int)
            or isinstance(end_char, bool)
            or start_char < 0
            or end_char <= start_char
            or end_char > len(raw_text)
            or raw_text[start_char:end_char] != source_text
        ):
            continue
        search_terms = tuple(dict.fromkeys(
            " ".join(value.split()).casefold()
            for value in item.get("search_terms_en") or []
            if isinstance(value, str) and value.strip()
        ))
        if not search_terms:
            continue
        expansion_spans.setdefault(source_id, []).append(
            {
                "source_text": source_text,
                "start_char": start_char,
                "end_char": end_char,
                "search_terms": search_terms,
                "term_type": item.get("term_type"),
            }
        )

    source_by_internal_id: dict[str, dict[str, Any]] = {}
    query_segments: list[MedicalQuerySegment] = []
    for position, source in enumerate(validated_segments, start=1):
        raw_text = source["text"]
        if not raw_text:
            continue
        collection_hints = (
            collection_hints_by_source_id.get(str(source["id"]))
            if collection_hints_by_source_id is not None
            else None
        )
        explicit_queries: list[str] = []
        seen_queries: set[str] = set()
        if str(source["id"]) in medical_term_query_source_ids:
            for expansion_span in expansion_spans.get(source["id"], []):
                for search_term in expansion_span["search_terms"]:
                    if search_term in seen_queries:
                        continue
                    seen_queries.add(search_term)
                    explicit_queries.append(search_term)
        if explicit_queries:
            for query_position, search_term in enumerate(explicit_queries, start=1):
                internal_id = f"q{position:04d}m{query_position:03d}"
                source_by_internal_id[internal_id] = source
                query_segments.append(
                    MedicalQuerySegment(
                        segment_id=internal_id,
                        raw_text=raw_text,
                        translated_text_en=search_term,
                        collection_hints=collection_hints,
                    )
                )
            continue
        internal_id = f"q{position:04d}"
        source_by_internal_id[internal_id] = source
        query_segments.append(
            MedicalQuerySegment(
                segment_id=internal_id,
                raw_text=raw_text,
                translated_text_en=translations.get(source["id"]),
                collection_hints=collection_hints,
            )
        )

    document = MedicalQueryDocument(segments=tuple(query_segments))
    resolution = medical_query_resolver.resolve(document)
    projected: dict[Any, list[dict[str, Any]]] = {
        source["id"]: [] for source in validated_segments
    }
    match_types = {
        "approved_alias": "approved_alias_candidate",
        "raw_similarity": "raw_similarity_candidate",
        "umls": "umls_dictionary_search",
        "ngram_fallback": "ngram_dictionary_fallback",
    }
    for candidate in resolution.candidates:
        source = source_by_internal_id.get(candidate.segment_id)
        if source is None:
            continue
        evidence = candidate.evidence
        annotation_group_id: str | None = None
        mapped_term_type: object = None
        if evidence.scope == "exact_raw_span":
            raw_span = evidence.raw_span
            if raw_span is None:
                continue
            source_text = raw_span.text
            start_char = raw_span.start_char
            end_char = raw_span.end_char
        else:
            source_text = source["text"]
            start_char = 0
            end_char = len(source_text)
            translated_span = evidence.translated_query_span
            if translated_span is None:
                continue
            search_identities = {
                " ".join(translated_span.text.split()).casefold(),
                " ".join(
                    str(candidate.dictionary_match.canonical_en or "").split()
                ).casefold(),
            } - {""}
            mapped_spans = {
                (
                    item["source_text"],
                    item["start_char"],
                    item["end_char"],
                    item.get("term_type"),
                )
                for item in expansion_spans.get(source["id"], [])
                if search_identities & set(item["search_terms"])
            }
            if len(mapped_spans) == 1:
                source_text, start_char, end_char, mapped_term_type = next(
                    iter(mapped_spans)
                )
            annotation_group_id = (
                f"translated:{translated_span.start_char}:"
                f"{translated_span.end_char}:{translated_span.text}"
            )
        match = candidate.dictionary_match
        match_type = match_types.get(candidate.route)
        if candidate.route == "raw_exact":
            match_type = (
                "official_exact"
                if candidate.review_status == "official"
                else "medical_query_raw_exact"
            )
        if match_type is None:
            continue
        output = {
            "source_text": source_text,
            "start_char": start_char,
            "end_char": end_char,
            "collection": match.collection,
            "entity_id": match.entity_id,
            "canonical_ko": match.canonical_ko,
            "canonical_en": match.canonical_en or "",
            "match_type": match_type,
            "review_status": candidate.review_status,
            "retrieval_score": match.retrieval_score,
        }
        if evidence.scope != "exact_raw_span":
            translated_span = evidence.translated_query_span
            if translated_span is not None:
                output["_search_term_en"] = translated_span.text
        if mapped_term_type:
            output["_term_type"] = mapped_term_type
        provenance_source = {
            "raw_exact": "RAW_EXACT",
            "approved_alias": "RAW_EXACT",
            "umls": "UMLS",
            "ngram_fallback": "NGRAM_FALLBACK",
        }.get(candidate.route)
        if provenance_source is not None:
            provenance: dict[str, Any] = {"source": provenance_source}
            if candidate.umls_provenance is not None:
                provenance.update(
                    {
                        "cui": candidate.umls_provenance.cui,
                        "semantic_types": list(
                            candidate.umls_provenance.semantic_types
                        ),
                        "similarity": candidate.umls_provenance.linking_score,
                    }
                )
            output["provenance"] = provenance
        if match.entity_id.startswith("drug:ingredient:"):
            output["entity_type"] = "ingredient"
        elif match.entity_id.startswith("drug:product:"):
            output["entity_type"] = "product"
        if annotation_group_id is not None:
            output["_annotation_group_id"] = annotation_group_id
        projected[source["id"]].append(output)
    return projected, resolution


def run_clinical_workflow(
    whisper_payload: dict[str, Any],
    *,
    retriever: Any | None,
    clinical_extractor: Any,
    query_expander: Any | None = None,
    medical_query_resolver: Any | None = None,
    preserve_unsupported: bool = False,
    include_query_resolution_summary: bool = False,
) -> dict[str, Any]:
    from .contracts import validate_whisper_payload
    from .query_expansion import run_query_expansion

    def telemetry_number(value: object, default: float = 0.0) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            return default
        return float(value)

    def telemetry_count(value: object) -> int:
        return value if type(value) is int and value >= 0 else 0

    validated_segments = validate_whisper_payload(whisper_payload)
    covered_spans: list[dict[str, Any]] = []
    staged_extract_record = getattr(clinical_extractor, "extract_record", None)
    staged_finalize_record = getattr(clinical_extractor, "finalize_record", None)
    staged_extraction = callable(staged_extract_record) and callable(
        staged_finalize_record
    )
    extracted_record_stage: dict[str, Any] | None = None
    clinical_record_stage_ms = 0.0

    def run_translation_stage() -> tuple[dict[str, Any], float]:
        started = time.perf_counter()
        expanded = run_query_expansion(
            query_expander,
            validated_segments,
            covered_spans=covered_spans,
        )
        return expanded, round((time.perf_counter() - started) * 1000, 3)

    def run_record_stage() -> tuple[dict[str, Any], float]:
        started = time.perf_counter()
        extracted = staged_extract_record(whisper_payload)
        return extracted, round((time.perf_counter() - started) * 1000, 3)

    # Translation and conversation-grounded field extraction use the same
    # immutable STT input and separate model clients, so only these two stages
    # are parallelized. Candidate adjudication still waits for retrieval.
    if staged_extraction:
        with ThreadPoolExecutor(max_workers=2) as executor:
            translation_future = executor.submit(run_translation_stage)
            record_future = executor.submit(run_record_stage)
            query_expansion, measured_translation_ms = translation_future.result()
            extracted_record_stage, clinical_record_stage_ms = record_future.result()
    else:
        query_expansion, measured_translation_ms = run_translation_stage()
    query_expansion_telemetry = query_expansion.pop("_telemetry", None)
    if not isinstance(query_expansion_telemetry, dict):
        query_expansion_telemetry = {}
    telemetry = {
        "translation_ms": telemetry_number(
            query_expansion_telemetry.get(
                "translation_ms",
                measured_translation_ms,
            ),
            measured_translation_ms,
        ),
        "translation_calls": telemetry_count(
            query_expansion_telemetry.get("translation_calls", 0)
        ),
        "umls_ms": 0.0,
        "dictionary_ms": 0.0,
        "vector_ms": 0.0,
        "exact_statement_count": 0,
        "vector_statement_count": 0,
        "search_cache_hit_count": 0,
        "vector_drug_terms_ms": 0.0,
        "vector_drug_terms_statement_count": 0,
        "vector_procedure_terms_ms": 0.0,
        "vector_procedure_terms_statement_count": 0,
        "vector_anatomy_terms_ms": 0.0,
        "vector_anatomy_terms_statement_count": 0,
        "vector_emergency_terms_ms": 0.0,
        "vector_emergency_terms_statement_count": 0,
        "clinical_extraction_ms": 0.0,
    }
    errors: list[dict[str, str]] = []
    if query_expansion.get("status") == "unavailable":
        errors.append(
            {
                "stage": "query_expansion",
                "code": str(
                    query_expansion.get("error_code")
                    or "QueryExpansionUnavailable"
                ),
                "detail": (
                    "Query expansion unavailable; dictionary-only fallback used"
                ),
            }
        )
    elif query_expansion.get("partial"):
        failed_segment_ids = query_expansion.get("failed_segment_ids")
        failed_segment_ids = (
            failed_segment_ids if isinstance(failed_segment_ids, list) else []
        )
        errors.append(
            {
                "stage": "query_expansion",
                "code": str(
                    query_expansion.get("error_code")
                    or "PartialTranslationFailure"
                ),
                "detail": (
                    "Translation unavailable for segments: "
                    + ", ".join(str(value) for value in failed_segment_ids)
                ),
            }
        )
    resolved_candidates_by_segment: dict[Any, list[dict[str, Any]]] | None = None
    query_resolution_summary: dict[str, Any] | None = None
    if medical_query_resolver is not None:
        resolver_mode = getattr(medical_query_resolver, "mode", None)
        try:
            clinical_record = (
                extracted_record_stage.get("clinical_record")
                if isinstance(extracted_record_stage, dict)
                and isinstance(extracted_record_stage.get("clinical_record"), dict)
                else {}
            )
            collection_hints = field_collection_hints_by_segment(
                clinical_record,
                segments=validated_segments,
            )
            chief_query_source_ids = frozenset(
                segment_id
                for segment_id, field_evidence in evidence_fields_by_segment(
                    clinical_record,
                    segments=validated_segments,
                ).items()
                if any(
                    evidence.field_id == "chief_complaint"
                    for evidence in field_evidence
                )
            )
            (
                projected_candidates_by_segment,
                query_resolution,
            ) = _resolved_candidates_for_pipeline(
                validated_segments,
                query_expansion,
                medical_query_resolver,
                collection_hints_by_source_id=collection_hints,
                medical_term_query_source_ids=chief_query_source_ids,
            )
            query_resolution_summary = {
                "schema_version": query_resolution.schema_version,
                "mode": query_resolution.mode,
                "status": query_resolution.status,
                "policy_version": query_resolution.policy_version,
                "fallback_used": query_resolution.fallback_used,
            }
            resolution_telemetry = getattr(query_resolution, "telemetry", None)
            if resolution_telemetry is not None:
                telemetry["umls_ms"] = telemetry_number(
                    getattr(resolution_telemetry, "umls_ms", 0.0)
                )
                telemetry["dictionary_ms"] = telemetry_number(
                    getattr(resolution_telemetry, "dictionary_ms", 0.0)
                )
                telemetry["vector_ms"] = telemetry_number(
                    getattr(resolution_telemetry, "vector_ms", 0.0)
                )
                telemetry["exact_statement_count"] = telemetry_count(
                    getattr(resolution_telemetry, "exact_statement_count", 0)
                )
                telemetry["vector_statement_count"] = telemetry_count(
                    getattr(resolution_telemetry, "vector_statement_count", 0)
                )
                telemetry["search_cache_hit_count"] = telemetry_count(
                    getattr(resolution_telemetry, "search_cache_hit_count", 0)
                )
                collection_ms = dict(
                    getattr(resolution_telemetry, "vector_collection_ms", ())
                )
                collection_statement_counts = dict(
                    getattr(
                        resolution_telemetry,
                        "vector_collection_statement_counts",
                        (),
                    )
                )
                for collection in (
                    "drug_terms",
                    "procedure_terms",
                    "anatomy_terms",
                    "emergency_terms",
                ):
                    telemetry[f"vector_{collection}_ms"] = telemetry_number(
                        collection_ms.get(collection, 0.0)
                    )
                    telemetry[
                        f"vector_{collection}_statement_count"
                    ] = telemetry_count(
                        collection_statement_counts.get(collection, 0)
                    )
            if query_resolution.mode != "shadow":
                resolved_candidates_by_segment = projected_candidates_by_segment
        except Exception as error:
            if resolver_mode != "shadow":
                # Retrieval augmentation must never erase a conversation-grounded
                # draft. Falling back to the legacy path keeps the clinical record
                # and locally available candidates while exposing only a bounded,
                # non-fatal resolution summary to v2 clients.
                resolved_candidates_by_segment = None
                query_resolution_summary = {
                    "schema_version": "medical-query-resolution-v1",
                    "mode": "umls_primary",
                    "status": "partial",
                    "policy_version": "medical-query-resolution-fallback-v1",
                    "fallback_used": True,
                }

    api3_document = run_api3(
        whisper_payload,
        retriever=retriever,
        max_candidates_per_span=5,
        query_expansion=query_expansion,
        resolved_candidates_by_segment=resolved_candidates_by_segment,
    )
    enriched_query_expansion = api3_document.pop("query_expansion", None)
    if isinstance(enriched_query_expansion, dict):
        query_expansion = enriched_query_expansion
    api2_document: dict[str, Any] | None = None
    clinical_extraction_started = time.perf_counter()
    try:
        api2_payload = _api2_payload(
            whisper_payload,
            api3_document,
            query_expansion,
        )
        if staged_extraction and extracted_record_stage is not None:
            api2_document = staged_finalize_record(
                extracted_record_stage,
                api2_payload,
            )
        else:
            api2_document = clinical_extractor.extract(api2_payload)
        if isinstance(api2_document, dict):
            stage_errors = api2_document.pop("stage_errors", None)
            if isinstance(stage_errors, list):
                for stage_error in stage_errors:
                    if not isinstance(stage_error, dict):
                        continue
                    stage = stage_error.get("stage")
                    code = stage_error.get("code")
                    detail = stage_error.get("detail")
                    if not isinstance(stage, str) or not isinstance(code, str):
                        continue
                    errors.append(
                        {
                            "stage": stage,
                            "code": code,
                            "detail": detail if isinstance(detail, str) else "",
                        }
                    )
            api2_document = _normalize_api2_document(
                api2_document,
                api3_document,
                preserve_unsupported=preserve_unsupported,
            )
        else:
            raise ValueError("clinical extractor returned an invalid contract")
    except Exception as error:
        api2_document = None
        errors.append(
            {
                "stage": "api2",
                "code": type(error).__name__,
                "detail": str(error),
            }
        )
    finally:
        finalization_ms = (time.perf_counter() - clinical_extraction_started) * 1000
        telemetry["clinical_extraction_ms"] = round(
            clinical_record_stage_ms + finalization_ms,
            3,
        )

    api3_status = (api3_document.get("metadata") or {}).get("processing_status")
    processing_status = (
        "partial" if errors or api3_status == "partial" else "completed"
    )
    candidate_decisions = _candidate_decisions(api2_document, api3_document)
    if api2_document is not None:
        api2_document["candidate_decisions"] = _api2_candidate_decisions(
            candidate_decisions
        )
    audit = _workflow_audit(api3_document, api2_document)
    api3_document.pop("metadata", None)
    if api2_document is not None:
        api2_document.pop("metadata", None)
    result = {
        "schema_version": SCHEMA_VERSION,
        "processing_status": processing_status,
        "query_expansion": query_expansion,
        "api3": api3_document,
        "api2": api2_document,
        "candidate_decisions": candidate_decisions,
        "audit": audit,
        "draft": build_draft(
            api2_document,
            api3_document,
            candidate_decisions,
            query_expansion.get("translated_segments")
            if isinstance(query_expansion.get("translated_segments"), list)
            else None,
        ),
        "errors": errors,
        "telemetry": {
            key: round(value, 3) if isinstance(value, float) else value
            for key, value in telemetry.items()
        },
    }
    if include_query_resolution_summary and query_resolution_summary is not None:
        result["query_resolution"] = query_resolution_summary
    return result

