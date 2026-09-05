from __future__ import annotations

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
    "outcome",
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
    "outcome": "outcome",
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
    translated_segments: list[dict[str, Any]] | None = None,
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
    translations = {
        str(item.get("segment_id")): str(
            item.get("translated_text_en") or ""
        ).strip()
        for item in translated_segments or []
        if isinstance(item, dict)
        and item.get("segment_id") is not None
        and str(item.get("translated_text_en") or "").strip()
    }

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
            source_texts = {
                source_text,
                str(segment.get("corrected_text") or ""),
                translations.get(str(segment.get("id")), ""),
            }
            if not any(
                compact_value in "".join(candidate.split())
                for candidate in source_texts
                if candidate
            ):
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
    for key in (
        "aggravating_factors",
        "alleviating_factors",
        "associated_symptoms",
        "pre_hospital_care",
    ):
        history[key] = _as_list(history.get(key))
    review_of_systems = record.get("review_of_systems")
    structured_ros = isinstance(review_of_systems, dict) and "text" in review_of_systems
    if structured_ros:
        review_of_systems["items"] = _as_list(review_of_systems.get("items"))
    else:
        record["review_of_systems"] = _as_list(review_of_systems)
    physical_examination = record.get("physical_examination")
    structured_physical_exam = (
        isinstance(physical_examination, dict)
        and "text" in physical_examination
    )
    if structured_physical_exam:
        physical_examination["findings"] = _as_list(
            physical_examination.get("findings")
        )
    else:
        record["physical_examination"] = _as_list(physical_examination)
    treatment_plan = record.get("treatment_plan")
    structured_treatment_plan = (
        isinstance(treatment_plan, dict) and "text" in treatment_plan
    )
    if structured_treatment_plan:
        treatment_plan["items"] = _as_list(treatment_plan.get("items"))
    else:
        record["treatment_plan"] = _as_list(treatment_plan)
    impression = record.get("impression")
    structured_impression = isinstance(impression, dict) and "text" in impression
    if structured_impression:
        impression["items"] = _as_list(impression.get("items"))
    else:
        record["impression"] = _as_list(impression)
    outcome = record.get("outcome")
    if not (isinstance(outcome, dict) and "information_status" in outcome):
        record.pop("outcome", None)
    segment_positions = {
        segment.get("id"): position for position, segment in enumerate(segments)
    }
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
        if is_symptom_review_answer and not structured_ros:
            if item not in record["review_of_systems"]:
                record["review_of_systems"].append(item)

    ros_values = {
        str(item.get("raw_value") or "")
        for item in (record["review_of_systems"] if not structured_ros else [])
        if isinstance(item, dict)
    }
    symptom_terms = ("소변", "기침", "열", "구토", "설사", "어지", "숨", "가슴", "통증")
    uncertainty_pattern = r"(?:모르|확실|것 같|기억)"
    for position, segment in enumerate(segments):
        if structured_ros:
            break
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
    past_history["previous_admissions"] = _as_list(
        past_history.get("previous_admissions")
    )
    past_history_has_model_text = bool(
        str(past_history.get("text") or "").strip()
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
        if past_history_has_model_text:
            break
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

    allergy = record.get("drug_allergy")
    has_structured_allergy = isinstance(allergy, dict) and bool(
        str(allergy.get("text") or "").strip()
        or _as_list(allergy.get("items"))
        or _as_list(allergy.get("specific_denials"))
        or any(
            str(atom.get("raw_value") or "").strip()
            for atom in _atomic_values(allergy.get("allergy_status"))
        )
    )
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
    if allergy_segment is not None and not has_structured_allergy:
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

    social_history = record.get("social_history")
    if not isinstance(social_history, dict):
        social_history = {}
        record["social_history"] = social_history
    smoking = social_history.get("smoking")
    smoking_has_model_text = isinstance(smoking, dict) and bool(
        str(smoking.get("text") or "").strip()
    )

    def confirmed_numeric_value(value: Any) -> float | None:
        for atom in _atomic_values(value):
            if atom.get("status") != "confirmed":
                continue
            number = atom.get("value")
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                continue
            if math.isfinite(float(number)) and float(number) >= 0:
                return float(number)
        return None

    if isinstance(smoking, dict) and smoking_has_model_text:
        packs_per_day = confirmed_numeric_value(smoking.get("packs_per_day"))
        cigarettes_per_day = confirmed_numeric_value(
            smoking.get("cigarettes_per_day")
        )
        duration_years = confirmed_numeric_value(smoking.get("duration_years"))
        if (
            packs_per_day is not None
            and cigarettes_per_day is not None
            and not math.isclose(
                packs_per_day,
                cigarettes_per_day / 20,
                rel_tol=0,
                abs_tol=0.001,
            )
        ):
            smoking["measurement_conflict"] = True
        effective_packs_per_day = packs_per_day
        formula = "packs_per_day * duration_years"
        if effective_packs_per_day is None and cigarettes_per_day is not None:
            effective_packs_per_day = cigarettes_per_day / 20
            formula = "cigarettes_per_day / 20 * duration_years"
        if effective_packs_per_day is not None and duration_years is not None:
            pack_years = round(effective_packs_per_day * duration_years, 3)
            smoking["pack_years"] = (
                int(pack_years) if pack_years.is_integer() else pack_years
            )
            smoking["pack_years_provenance"] = {
                "basis_type": "STRUCTURED_MEASUREMENT",
                "status": "APPROVED",
                "formula": formula,
                "cigarettes_per_pack": 20,
            }

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
    if smoking_segment is not None and not smoking_has_model_text:
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
    medications_has_model_text = bool(
        str(medications.get("text") or "").strip()
    )

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
    if best_answer is not None and not medications_has_model_text:
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
        and (
            (
                allergy.get("status") in {"confirmed", "needs_confirmation"}
                and isinstance(allergy.get("raw_value"), str)
                and bool(allergy["raw_value"].strip())
            )
            or bool(str(allergy.get("text") or "").strip())
            or bool(_as_list(allergy.get("items")))
            or bool(_as_list(allergy.get("specific_denials")))
            or any(
                str(atom.get("raw_value") or "").strip()
                for atom in _atomic_values(allergy.get("allergy_status"))
            )
        )
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


def _hpi_draft_field(
    source: Any,
    api3_segments: list[dict[str, Any]],
    *,
    candidate_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(source, dict):
        return _draft_field(
            source,
            api3_segments,
            candidate_decisions=candidate_decisions,
        )

    text = str(source.get("text") or "").strip()
    atoms = list(_atomic_values(source))
    grounded_atoms = [
        atom for atom in atoms if str(atom.get("raw_value") or "").strip()
    ]
    evidence: list[dict[str, Any]] = []
    for atom in grounded_atoms:
        normalized = _evidence_with_segment_id(atom.get("evidence"), api3_segments)
        if normalized is not None and normalized not in evidence:
            evidence.append(normalized)

    if text:
        needs_review = not grounded_atoms or any(
            atom.get("status") in {"needs_confirmation", "asked_but_unanswered"}
            for atom in atoms
        )
        return {
            "value": text,
            "status": "needs_review" if needs_review else "filled",
            "evidence": evidence,
        }
    if not grounded_atoms:
        return {"value": "", "status": "empty", "evidence": []}
    return _draft_field(
        source,
        api3_segments,
        candidate_decisions=candidate_decisions,
    )


def _past_history_draft_field(
    source: Any,
    api3_segments: list[dict[str, Any]],
    *,
    candidate_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(source, dict):
        return _draft_field(
            source,
            api3_segments,
            candidate_decisions=candidate_decisions,
        )

    text = str(source.get("text") or "").strip()
    atoms = list(_atomic_values(source))
    grounded_atoms = [
        atom for atom in atoms if str(atom.get("raw_value") or "").strip()
    ]
    evidence: list[dict[str, Any]] = []
    for atom in grounded_atoms:
        normalized = _evidence_with_segment_id(atom.get("evidence"), api3_segments)
        if normalized is not None and normalized not in evidence:
            evidence.append(normalized)

    if text:
        displays_no_history = bool(
            re.fullmatch(
                r"(?:NONE|특이\s*과거력\s*없음)[.!]?",
                text,
                re.IGNORECASE,
            )
        )

        def explicitly_denied(value: Any) -> bool:
            status_atoms = [
                atom
                for atom in _atomic_values(value)
                if str(atom.get("raw_value") or "").strip()
            ]
            return bool(status_atoms) and all(
                atom.get("status") == "confirmed"
                and re.search(
                    r"(?:없|아니|그런\s*적|하지\s*않|안\s+\S+|"
                    r"\bno\b|\bnone\b|\bden(?:y|ies|ied)\b)",
                    str(atom.get("raw_value") or ""),
                    re.IGNORECASE,
                )
                for atom in status_atoms
            )

        complete_denial = all(
            explicitly_denied(source.get(key))
            for key in (
                "medical_history_status",
                "surgical_history_status",
                "admission_history_status",
            )
        )
        needs_review = not grounded_atoms or any(
            atom.get("status") in {"needs_confirmation", "asked_but_unanswered"}
            for atom in atoms
        )
        if displays_no_history and not complete_denial:
            needs_review = True
        return {
            "value": text,
            "status": "needs_review" if needs_review else "filled",
            "evidence": evidence,
        }
    return _draft_field(
        source,
        api3_segments,
        candidate_decisions=candidate_decisions,
    )


def _medications_draft_field(
    source: Any,
    api3_segments: list[dict[str, Any]],
    *,
    candidate_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(source, dict):
        return _draft_field(
            source,
            api3_segments,
            candidate_decisions=candidate_decisions,
        )

    text = str(source.get("text") or "").strip()
    atoms = list(_atomic_values(source))
    grounded_atoms = [
        atom for atom in atoms if str(atom.get("raw_value") or "").strip()
    ]
    evidence: list[dict[str, Any]] = []
    for atom in grounded_atoms:
        normalized = _evidence_with_segment_id(atom.get("evidence"), api3_segments)
        if normalized is not None and normalized not in evidence:
            evidence.append(normalized)

    if text:
        needs_review = not grounded_atoms or any(
            atom.get("status") in {"needs_confirmation", "asked_but_unanswered"}
            for atom in atoms
        )
        medication_items = _deduplicated(
            " ".join(str(atom.get("raw_value") or "").split()).casefold()
            for atom in _atomic_values(source.get("items"))
            if str(atom.get("raw_value") or "").strip()
        )
        display_items = [
            item.strip() for item in text.split(",") if item.strip()
        ]
        if len(display_items) < len(medication_items):
            needs_review = True
        displays_no_medication = bool(
            re.fullmatch(
                r"(?:NONE|복용(?:\s*중인)?\s*약\s*없음|복용약\s*없음)[.!]?",
                text,
                re.IGNORECASE,
            )
        )
        if displays_no_medication:
            segments = [
                segment for segment in api3_segments if isinstance(segment, dict)
            ]
            segment_positions = {
                str(segment.get("id")): position
                for position, segment in enumerate(segments)
                if segment.get("id") is not None
            }
            status_atoms = [
                atom
                for atom in _atomic_values(source.get("medication_status"))
                if str(atom.get("raw_value") or "").strip()
            ]
            broad_denial = False
            for atom in status_atoms:
                raw_value = str(atom.get("raw_value") or "").strip()
                is_denial = atom.get("status") == "confirmed" and bool(
                    re.search(
                        r"(?:없|아니|먹지\s*않|복용하지\s*않|안\s+\S+|"
                        r"\bno\b|\bnone\b|\bden(?:y|ies|ied)\b)",
                        raw_value,
                        re.IGNORECASE,
                    )
                )
                normalized = _evidence_with_segment_id(
                    atom.get("evidence"), api3_segments
                )
                position = (
                    segment_positions.get(str(normalized.get("segment_id")))
                    if normalized is not None
                    else None
                )
                previous_text = (
                    str(segments[position - 1].get("raw_text") or "")
                    if isinstance(position, int) and position > 0
                    else ""
                )
                broad_question = bool(
                    re.search(
                        r"(?:평소|현재)?.*(?:복용약|드시는\s*약|먹는\s*약|"
                        r"복용하는\s*약|복용\s*중인\s*약)|"
                        r"(?:what|any)\s+medications?|medications?\s+do\s+you\s+take",
                        previous_text,
                        re.IGNORECASE,
                    )
                )
                broad_statement = bool(
                    re.search(
                        r"(?:복용약|먹는\s*약|복용\s*중인\s*약).*(?:없|none|no)",
                        raw_value,
                        re.IGNORECASE,
                    )
                )
                if is_denial and (broad_question or broad_statement):
                    broad_denial = True
                    break
            positive_items = any(
                str(atom.get("raw_value") or "").strip()
                for atom in _atomic_values(source.get("items"))
            )
            if not broad_denial or positive_items:
                needs_review = True
        return {
            "value": text,
            "status": "needs_review" if needs_review else "filled",
            "evidence": evidence,
        }
    return _draft_field(
        source,
        api3_segments,
        candidate_decisions=candidate_decisions,
    )


def _allergy_draft_field(
    source: Any,
    api3_segments: list[dict[str, Any]],
    *,
    candidate_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(source, dict) or "text" not in source:
        return _draft_field(
            source,
            api3_segments,
            candidate_decisions=candidate_decisions,
        )

    text = str(source.get("text") or "").strip()
    atoms = list(_atomic_values(source))
    grounded_atoms = [
        atom for atom in atoms if str(atom.get("raw_value") or "").strip()
    ]
    evidence: list[dict[str, Any]] = []
    for atom in grounded_atoms:
        normalized = _evidence_with_segment_id(atom.get("evidence"), api3_segments)
        if normalized is not None and normalized not in evidence:
            evidence.append(normalized)

    if not text:
        if not grounded_atoms:
            return {"value": "미확인", "status": "unknown", "evidence": []}
        return _draft_field(
            source,
            api3_segments,
            candidate_decisions=candidate_decisions,
        )

    needs_review = not grounded_atoms or any(
        atom.get("status") in {"needs_confirmation", "asked_but_unanswered"}
        for atom in atoms
    )
    if ";" in text:
        needs_review = True
    structured_items = [
        item
        for item in _as_list(source.get("items"))
        if isinstance(item, dict)
    ]
    allowed_allergy_types = {
        "drug",
        "contrast media",
        "latex",
        "food",
        "other",
    }
    for item in structured_items:
        allergy_type = str(item.get("allergy_type") or "").strip().casefold()
        has_allergen = any(
            str(atom.get("raw_value") or "").strip()
            for atom in _atomic_values(item.get("allergen"))
        )
        has_reaction = any(
            str(atom.get("raw_value") or "").strip()
            for atom in _atomic_values(item.get("reaction"))
        )
        if allergy_type not in allowed_allergy_types or not has_allergen:
            needs_review = True
        if has_reaction and not has_allergen:
            needs_review = True
    positive_items = [
        item
        for item in structured_items
        if any(
            str(atom.get("raw_value") or "").strip()
            for atom in _atomic_values(item.get("allergen"))
        )
    ]
    display_items = [item.strip() for item in text.split(",") if item.strip()]
    if len(display_items) < len(positive_items):
        needs_review = True

    displays_no_allergy = bool(
        re.fullmatch(
            r"(?:NONE|알레르기\s*없음|특이\s*알레르기\s*없음|"
            r"no\s+known\s+allerg(?:y|ies)|no\s+allerg(?:y|ies))[.!]?",
            text,
            re.IGNORECASE,
        )
    )
    if displays_no_allergy:
        segments = [
            segment for segment in api3_segments if isinstance(segment, dict)
        ]
        segment_positions = {
            str(segment.get("id")): position
            for position, segment in enumerate(segments)
            if segment.get("id") is not None
        }
        broad_denial = False
        for atom in _atomic_values(source.get("allergy_status")):
            raw_value = str(atom.get("raw_value") or "").strip()
            if atom.get("status") != "confirmed" or not re.search(
                r"(?:없|아니|해당\s*없|\bno\b|\bnone\b|\bden(?:y|ies|ied)\b)",
                raw_value,
                re.IGNORECASE,
            ):
                continue
            normalized = _evidence_with_segment_id(
                atom.get("evidence"), api3_segments
            )
            position = (
                segment_positions.get(str(normalized.get("segment_id")))
                if normalized is not None
                else None
            )
            previous_text = (
                str(segments[position - 1].get("raw_text") or "").strip()
                if isinstance(position, int) and position > 0
                else ""
            )
            broad_question = bool(
                re.search(
                    r"^(?:혹시\s*)?(?:(?:약(?:물)?|음식)(?:이나|이나\s+음식|·|,|과|와)\s*)?"
                    r"알(?:레르기|러지)|^(?:do\s+you\s+have|any)\b.*\ballerg",
                    previous_text,
                    re.IGNORECASE,
                )
            )
            broad_statement = bool(
                re.search(
                    r"^(?:저는\s*)?알(?:레르기|러지).*(?:없|no|none)",
                    raw_value,
                    re.IGNORECASE,
                )
            )
            if broad_question or broad_statement:
                broad_denial = True
                break
        if not broad_denial or positive_items:
            needs_review = True

    return {
        "value": text,
        "status": "needs_review" if needs_review else "filled",
        "evidence": evidence,
    }


def _social_history_draft_field(
    source: Any,
    api3_segments: list[dict[str, Any]],
    *,
    candidate_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(source, dict) or "text" not in source:
        return _draft_field(
            source,
            api3_segments,
            empty_when_missing=True,
            candidate_decisions=candidate_decisions,
        )

    text = str(source.get("text") or "").strip()
    atoms = list(_atomic_values(source))
    grounded_atoms = [
        atom for atom in atoms if str(atom.get("raw_value") or "").strip()
    ]
    evidence: list[dict[str, Any]] = []
    for atom in grounded_atoms:
        normalized = _evidence_with_segment_id(atom.get("evidence"), api3_segments)
        if normalized is not None and normalized not in evidence:
            evidence.append(normalized)

    if not text:
        if not grounded_atoms:
            return {"value": "미확인", "status": "unknown", "evidence": []}
        has_structured_child = any(
            isinstance(source.get(key), dict) and "text" in source[key]
            for key in ("smoking", "alcohol")
        )
        fallback = _draft_field(
            source,
            api3_segments,
            candidate_decisions=candidate_decisions,
        )
        if has_structured_child:
            fallback["status"] = "needs_review"
        return fallback

    needs_review = not grounded_atoms or any(
        atom.get("status") in {"needs_confirmation", "asked_but_unanswered"}
        for atom in atoms
    )
    smoking = source.get("smoking")
    alcohol = source.get("alcohol")
    smoking_text = (
        str(smoking.get("text") or "").strip()
        if isinstance(smoking, dict)
        else ""
    )
    alcohol_text = (
        str(alcohol.get("text") or "").strip()
        if isinstance(alcohol, dict)
        else ""
    )
    if smoking_text and smoking_text not in text:
        needs_review = True
    if alcohol_text and alcohol_text not in text:
        needs_review = True
    if re.search(r"\b(?:heavy|high[- ]risk)\s+drinker\b", text, re.IGNORECASE):
        needs_review = True

    if isinstance(smoking, dict):
        if smoking.get("measurement_conflict") is True:
            needs_review = True
        state = str(smoking.get("state") or "").strip().casefold()
        allowed_states = {"current smoker", "former smoker", "never smoker", ""}
        if state not in allowed_states:
            needs_review = True
        status_text = " ".join(
            str(atom.get("raw_value") or "").strip()
            for atom in _atomic_values(smoking.get("smoking_status"))
        )
        if state == "current smoker" and re.search(
            r"(?:안\s*피|피우지\s*않|흡연하지\s*않|금연|\bnot\s+smok|\bno\b)",
            status_text,
            re.IGNORECASE,
        ):
            needs_review = True
        if state == "former smoker" and not (
            any(
                str(atom.get("raw_value") or "").strip()
                for atom in _atomic_values(smoking.get("quit_years"))
            )
            or re.search(r"(?:끊|금연|피우다가|used\s+to\s+smoke|former)", status_text, re.IGNORECASE)
        ):
            needs_review = True
        if state == "never smoker" and not re.search(
            r"(?:한\s*번도|전혀|평생|피운\s*적\s*없|\bnever\b)",
            status_text,
            re.IGNORECASE,
        ):
            needs_review = True

        pack_years = smoking.get("pack_years")
        displayed_pack_years = re.search(
            r"(?<!\d)(\d+(?:\.\d+)?)\s*PY\b",
            smoking_text or text,
            re.IGNORECASE,
        )
        if isinstance(pack_years, (int, float)) and not isinstance(pack_years, bool):
            if not displayed_pack_years or not math.isclose(
                float(displayed_pack_years.group(1)),
                float(pack_years),
                rel_tol=0,
                abs_tol=0.001,
            ):
                needs_review = True
        elif displayed_pack_years:
            needs_review = True

    return {
        "value": text,
        "status": "needs_review" if needs_review else "filled",
        "evidence": evidence,
    }


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

    def annotation_supports_atom(
        annotation: dict[str, Any],
        raw_value: str,
    ) -> bool:
        atom_identity = " ".join(raw_value.split()).casefold()
        annotation_identities = [
            str((annotation.get("source_span") or {}).get("text") or ""),
            *[
                str(term)
                for term in annotation.get("search_terms_en", [])
                if isinstance(term, str)
            ],
        ]
        return any(
            identity
            and (
                identity in atom_identity
                or atom_identity in identity
            )
            for value in annotation_identities
            if (identity := " ".join(value.split()).casefold())
        )

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
                if not annotation_supports_atom(annotation, raw_value):
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
    source = record.get("review_of_systems")
    if isinstance(source, dict) and "text" in source:
        text = str(source.get("text") or "").strip()
        items = [
            item
            for item in _as_list(source.get("items"))
            if isinstance(item, dict)
        ]
        if not text and not items:
            return {"value": "", "status": "empty", "evidence": []}

        evidence_values: list[dict[str, Any]] = []
        needs_review = not text or not items
        assertions: list[str] = []
        supported_texts_by_item: list[list[str]] = []
        segments_by_id = {
            str(segment.get("id")): segment
            for segment in api3_segments
            if isinstance(segment, dict) and segment.get("id") is not None
        }
        for item in items:
            assertion = str(item.get("assertion") or "").strip().upper()
            symptom_atoms = [
                atom
                for atom in _atomic_values(item.get("symptom"))
                if str(atom.get("raw_value") or "").strip()
            ]
            if assertion not in {"PRESENT", "DENIED", "UNCERTAIN"}:
                needs_review = True
            assertions.append(assertion)
            supported_texts: list[str] = []
            if not symptom_atoms:
                needs_review = True
                supported_texts_by_item.append(supported_texts)
                continue
            for atom in symptom_atoms:
                evidence = _evidence_with_segment_id(
                    atom.get("evidence"), api3_segments
                )
                if evidence is not None and evidence not in evidence_values:
                    evidence_values.append(evidence)
                raw_value = str(atom.get("raw_value") or "").strip()
                if raw_value:
                    supported_texts.append(raw_value)
                segment = (
                    segments_by_id.get(str(evidence.get("segment_id")))
                    if evidence is not None
                    else None
                )
                if segment is not None:
                    translated_text = str(
                        segment.get("translated_text_en") or ""
                    ).strip()
                    if translated_text:
                        supported_texts.append(translated_text)
                    for annotation in segment.get("annotations", []):
                        if not isinstance(annotation, dict):
                            continue
                        source_text = str(
                            (annotation.get("source_span") or {}).get("text")
                            or ""
                        ).strip()
                        if source_text and not (
                            source_text.casefold() in raw_value.casefold()
                            or raw_value.casefold() in source_text.casefold()
                        ):
                            continue
                        supported_texts.extend(
                            str(term).strip()
                            for term in annotation.get("search_terms_en", [])
                            if str(term).strip()
                        )
                        supported_texts.extend(
                            str(candidate.get("canonical_en") or "").strip()
                            for candidate in annotation.get("candidates", [])
                            if isinstance(candidate, dict)
                            and str(candidate.get("canonical_en") or "").strip()
                        )
                inferred_assertion = {
                    "positive": "PRESENT",
                    "negative": "DENIED",
                    "uncertain": "UNCERTAIN",
                }[_ros_assertion(atom)]
                if assertion != inferred_assertion:
                    needs_review = True
                if atom.get("status") in {
                    "needs_confirmation",
                    "asked_but_unanswered",
                }:
                    needs_review = True
            if assertion == "UNCERTAIN":
                needs_review = True
            supported_texts_by_item.append(supported_texts)

        entries = [entry.strip() for entry in text.split(",") if entry.strip()]
        if len(entries) != len(items):
            needs_review = True
        expected_symbols = {
            "PRESENT": "+",
            "DENIED": "-",
            "UNCERTAIN": "?",
        }
        seen_labels: set[str] = set()
        for entry, assertion, supported_texts in zip(
            entries, assertions, supported_texts_by_item
        ):
            match = re.fullmatch(r".+\(([+?]|-)\)", entry)
            if (
                match is None
                or expected_symbols.get(assertion) != match.group(1)
            ):
                needs_review = True
            label = re.sub(r"\(([+?]|-)\)$", "", entry).strip()
            label_identity = " ".join(label.split()).casefold()
            if label_identity in seen_labels:
                needs_review = True
            seen_labels.add(label_identity)
            normalized_support = [
                " ".join(value.split()).casefold()
                for value in supported_texts
                if value.strip()
            ]
            if re.search(r"[ㄱ-ㅎㅏ-ㅣ가-힣]", label) or not any(
                label_identity in value or value in label_identity
                for value in normalized_support
            ):
                needs_review = True
            if re.search(
                r"(?:\d+(?:\.\d+)?\s*(?:일|주|개월|년|시간|분)"
                r"(?:\s*(?:전|동안|째|부터))?|"
                r"\bfor\s+\d+|\bsince\b|\bNRS\b|"
                r"\b(?:severe|mild|moderate|yellow|green|bloody|purulent)\b|"
                r"심(?:한|하게)|경증|중등도)",
                label,
                re.IGNORECASE,
            ):
                needs_review = True
        return {
            "value": text,
            "status": "needs_review" if needs_review else "filled",
            "evidence": evidence_values,
        }

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


_PHYSICAL_EXAM_SYSTEMS = frozenset(
    {
        "General",
        "HEENT",
        "Chest",
        "Abdomen",
        "Back / Spine",
        "Extremities / Musculoskeletal",
        "Neurology",
        "Other",
    }
)
_PHYSICAL_EXAM_CONTEXT_RE = re.compile(
    r"(?:청진|촉진|타진|시진|진찰|검사상|관찰|확인|소견|"
    r"눌러|누를|압통|반발통|호흡음|폐소리|심음|수포음|천명|"
    r"동공|반사|의식|근력|마비|감각|부종|청색증|변형|가동\s*범위|"
    r"cva\s*tenderness|gcs|alert|lethargic|ill-looking|"
    r"auscultat|palpat|percuss|inspect|exam(?:ination)?|observ|"
    r"tenderness|rebound|breath\s*sounds?|rales?|wheez|murmur|"
    r"pupil|reflex|motor\s*grade|hemiparesis|pitting\s*edema)",
    re.IGNORECASE,
)
_VITAL_ONLY_RE = re.compile(
    r"(?:\bBP\b|\bHR\b|\bRR\b|\bBT\b|SpO[₂2]|혈압|맥박|"
    r"호흡수|체온|산소포화도)\s*[:=]?\s*\d",
    re.IGNORECASE,
)


def _physical_exam_has_objective_context(
    segment: dict[str, Any],
    previous_segment: dict[str, Any] | None,
) -> bool:
    current = " ".join(
        str(segment.get(key) or "")
        for key in ("raw_text", "corrected_text", "translated_text_en")
    )
    previous = " ".join(
        str(previous_segment.get(key) or "")
        for key in ("raw_text", "corrected_text", "translated_text_en")
    ) if previous_segment is not None else ""
    if _VITAL_ONLY_RE.search(current) and not _PHYSICAL_EXAM_CONTEXT_RE.search(
        current
    ):
        return False
    return bool(
        _PHYSICAL_EXAM_CONTEXT_RE.search(current)
        or _PHYSICAL_EXAM_CONTEXT_RE.search(previous)
    )


def _physical_exam_draft_field(
    source: Any,
    api3_segments: list[dict[str, Any]],
    *,
    candidate_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not (isinstance(source, dict) and "text" in source):
        return _draft_field(
            source,
            api3_segments,
            candidate_decisions=candidate_decisions,
            empty_when_missing=True,
        )

    text = str(source.get("text") or "").strip()
    findings = [
        finding
        for finding in _as_list(source.get("findings"))
        if isinstance(finding, dict)
    ]
    if not text and not findings:
        return {"value": "", "status": "empty", "evidence": []}

    segments_by_id = {
        str(segment.get("id")): segment
        for segment in api3_segments
        if isinstance(segment, dict) and segment.get("id") is not None
    }
    positions = {
        str(segment.get("id")): index
        for index, segment in enumerate(api3_segments)
        if isinstance(segment, dict) and segment.get("id") is not None
    }
    evidence_values: list[dict[str, Any]] = []
    validation_reasons: list[str] = []
    if not text or not findings:
        validation_reasons.append("text_and_findings_must_coexist")

    systems_in_text = {
        line.split(":", 1)[0].strip()
        for line in text.splitlines()
        if ":" in line
    }
    if text and not systems_in_text:
        validation_reasons.append("system_prefix_missing")

    for finding in findings:
        system = str(finding.get("system") or "").strip()
        assertion = str(finding.get("assertion") or "").strip().upper()
        fact_type = str(finding.get("fact_type") or "UNKNOWN").strip().upper()
        finding_atoms = [
            atom
            for atom in _atomic_values(finding.get("finding"))
            if str(atom.get("raw_value") or "").strip()
        ]
        if system not in _PHYSICAL_EXAM_SYSTEMS:
            validation_reasons.append("unsupported_system")
        if system and system not in systems_in_text:
            validation_reasons.append("system_not_rendered")
        if assertion not in {"PRESENT", "NONE", "UNCERTAIN"}:
            validation_reasons.append("unsupported_assertion")
        if fact_type != "EXAM":
            validation_reasons.append("unresolved_fact_type")
        if not finding_atoms:
            validation_reasons.append("finding_without_evidence")
            continue
        for atom in finding_atoms:
            evidence = _evidence_with_segment_id(
                atom.get("evidence"), api3_segments
            )
            if evidence is not None and evidence not in evidence_values:
                evidence_values.append(evidence)
            segment_id = str(evidence.get("segment_id")) if evidence else ""
            segment = segments_by_id.get(segment_id)
            position = positions.get(segment_id)
            previous_segment = (
                api3_segments[position - 1]
                if isinstance(position, int) and position > 0
                else None
            )
            if segment is None or not _physical_exam_has_objective_context(
                segment, previous_segment
            ):
                validation_reasons.append("objective_exam_context_missing")

            inferred_assertion = {
                "positive": "PRESENT",
                "negative": "NONE",
                "uncertain": "UNCERTAIN",
            }[_ros_assertion(atom)]
            if assertion != inferred_assertion:
                validation_reasons.append("assertion_mismatch")
            if assertion == "UNCERTAIN" or atom.get("status") in {
                "needs_confirmation",
                "asked_but_unanswered",
            }:
                validation_reasons.append("uncertain_finding")

    unique_reasons = list(dict.fromkeys(validation_reasons))
    return {
        "value": text,
        "status": "needs_review" if unique_reasons else "filled",
        "evidence": evidence_values,
        **({"_validation_reasons": unique_reasons} if unique_reasons else {}),
    }


_TREATMENT_PLAN_CATEGORIES = frozenset(
    {
        "Diagnostic Workup",
        "Medication / Procedure",
        "Consultation",
        "Disposition / Safety-netting",
    }
)
_TREATMENT_PLAN_STATUS_ASSERTION = {
    "PLANNED": "PRESENT",
    "ORDERED": "PRESENT",
    "COMPLETED": "PRESENT",
    "CANCELED": "DENIED",
    "REFUSED": "DENIED",
    "CONDITIONAL": "UNCERTAIN",
}
_TREATMENT_PLAN_CONTEXT_RE = re.compile(
    r"(?:하겠습니다|진행|시행|촬영|검사(?:할|하|를)|투여|처방|"
    r"금식\s*유지|협진|의뢰|입원|퇴원|전원|관찰|재평가|"
    r"결정(?:할|하)|보내드|적용|봉합|소독|예약|거부|취소|"
    r"will\b|plan(?:ned)?\b|order(?:ed)?\b|perform|administer|"
    r"prescri|consult|admission|admit|discharg|transfer|observ|"
    r"re-?evaluat|follow-?up|completed|cancell|refus|pending)",
    re.IGNORECASE,
)
def _impression_draft_field(
    source: Any,
    api3_segments: list[dict[str, Any]],
    *,
    candidate_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not (isinstance(source, dict) and "text" in source):
        return _draft_field(
            source,
            api3_segments,
            candidate_decisions=candidate_decisions,
            empty_when_missing=True,
        )

    text = str(source.get("text") or "").strip()
    items = [
        item
        for item in _as_list(source.get("items"))
        if isinstance(item, dict)
    ]
    if not text and not items:
        return {"value": "", "status": "empty", "evidence": []}

    evidence: list[dict[str, Any]] = []
    needs_review = not text or not items
    allowed_certainties = {"CONFIRMED", "SUSPECTED", "RULE_OUT", "EXCLUDED"}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < len(items):
        needs_review = True

    for index, item in enumerate(items):
        certainty = str(item.get("certainty") or "").strip().upper()
        fact_type = str(item.get("fact_type") or "UNKNOWN").strip().upper()
        diagnosis_atoms = [
            atom
            for atom in _atomic_values(item.get("diagnosis"))
            if str(atom.get("raw_value") or "").strip()
        ]
        if certainty not in allowed_certainties or not diagnosis_atoms:
            needs_review = True
            continue
        if fact_type != "ASSESSMENT":
            needs_review = True
        if certainty in {"SUSPECTED", "RULE_OUT"}:
            needs_review = True
        for atom in diagnosis_atoms:
            normalized = _evidence_with_segment_id(
                atom.get("evidence"), api3_segments
            )
            if normalized is not None and normalized not in evidence:
                evidence.append(normalized)
            expected_status = (
                "needs_confirmation"
                if certainty in {"SUSPECTED", "RULE_OUT"}
                else "confirmed"
            )
            if atom.get("status") != expected_status:
                needs_review = True
        if index < len(lines):
            has_rule_out_prefix = bool(
                re.match(r"^R/O(?:\s+|$)", lines[index], re.IGNORECASE)
            )
            if certainty in {"SUSPECTED", "RULE_OUT"} and not has_rule_out_prefix:
                needs_review = True
            if certainty == "CONFIRMED" and has_rule_out_prefix:
                needs_review = True

    return {
        "value": text,
        "status": "needs_review" if needs_review else "filled",
        "evidence": evidence,
    }


_OUTCOME_LABELS = {
    "Discharge": "귀가",
    "Admission": "입원",
    "Transfer": "전원",
    "Death": "사망",
    "Other": "기타",
}


def _outcome_draft_field(
    source: Any,
    api3_segments: list[dict[str, Any]],
    *,
    candidate_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not (isinstance(source, dict) and "information_status" in source):
        return _draft_field(
            source,
            api3_segments,
            candidate_decisions=candidate_decisions,
            empty_when_missing=True,
        )

    information_status = str(source.get("information_status") or "").strip()
    category = str(source.get("category") or "").strip()
    text = str(source.get("text") or "").strip()
    decision_atoms = [
        atom
        for atom in _atomic_values(source.get("decision"))
        if str(atom.get("raw_value") or "").strip()
    ]
    detail_atoms = [
        atom
        for atom in _atomic_values(source.get("detail"))
        if str(atom.get("raw_value") or "").strip()
    ]
    evidence: list[dict[str, Any]] = []
    for atom in [*decision_atoms, *detail_atoms]:
        normalized = _evidence_with_segment_id(atom.get("evidence"), api3_segments)
        if normalized is not None and normalized not in evidence:
            evidence.append(normalized)

    if information_status == "NOT_ASSESSED" and not decision_atoms:
        return {"value": "", "status": "empty", "evidence": []}
    if information_status == "UNCERTAIN":
        return {
            "value": "",
            "status": "needs_review",
            "evidence": evidence,
        }

    expected_text = _OUTCOME_LABELS.get(category)
    needs_review = (
        information_status != "PRESENT"
        or str(source.get("fact_type") or "UNKNOWN").strip().upper() != "OUTCOME"
        or expected_text is None
        or not decision_atoms
        or any(atom.get("status") != "confirmed" for atom in decision_atoms)
        or (bool(text) and text != expected_text)
    )
    decision_text = " ".join(
        str(atom.get("raw_value") or "") for atom in decision_atoms
    )
    conditional = bool(
        re.search(
            r"(?:수도\s*있|가능성|고려|결과.{0,12}(?:보고|따라)|"
            r"경과.{0,12}(?:보고|따라)|\bmay\b|\bmight\b|\bconsider|"
            r"\bdepending\b|\bif\b)",
            decision_text,
            re.IGNORECASE,
        )
    )
    if conditional:
        return {
            "value": "",
            "status": "needs_review",
            "evidence": evidence,
        }
    if category == "Death" and not re.search(
        r"(?:사망.{0,8}(?:확인|선고)|(?:확인|선고).{0,8}사망|"
        r"pronounc(?:e|ed).{0,8}dead|confirmed.{0,8}death|\bexpired\b)",
        decision_text,
        re.IGNORECASE,
    ):
        return {
            "value": "",
            "status": "needs_review",
            "evidence": evidence,
        }
    if category == "Other" and not detail_atoms:
        needs_review = True

    return {
        "value": expected_text or text,
        "status": "needs_review" if needs_review else "filled",
        "evidence": evidence,
    }


_PLAN_TOKEN_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "after",
        "before",
        "for",
        "of",
        "on",
        "the",
        "to",
        "will",
        "we",
        "perform",
        "administer",
        "planned",
        "ordered",
        "completed",
    }
)


def _plan_tokens(value: Any) -> set[str]:
    text = str(value or "").casefold()
    text = re.sub(r"\bblood\s+tests?\b|\blaboratory\s+tests?\b|\blab\b", " bloodtest ", text)
    text = re.sub(r"\bgeneral\s+surgery\b|\bgs\b", " gs ", text)
    text = re.sub(r"\bx[ -]?ray\b", " xray ", text)
    tokens: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", text):
        if token in _PLAN_TOKEN_STOPWORDS:
            continue
        normalized = token[:-1] if len(token) > 4 and token.endswith("s") else token
        if len(normalized) > 1:
            tokens.add(normalized)
    return tokens


def _plan_display_action_supported(
    action_text: str,
    supported_texts: list[str],
) -> bool:
    action_tokens = _plan_tokens(action_text)
    if not action_tokens:
        return False
    for supported in supported_texts:
        support_tokens = _plan_tokens(supported)
        if not support_tokens:
            continue
        coverage = len(action_tokens & support_tokens) / len(action_tokens)
        if coverage >= 0.6:
            return True
    return False


def _treatment_plan_draft_field(
    source: Any,
    api3_segments: list[dict[str, Any]],
    *,
    candidate_decisions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not (isinstance(source, dict) and "text" in source):
        return _draft_field(
            source,
            api3_segments,
            candidate_decisions=candidate_decisions,
            empty_when_missing=True,
        )

    text = str(source.get("text") or "").strip()
    items = [
        item
        for item in _as_list(source.get("items"))
        if isinstance(item, dict)
    ]
    if not text and not items:
        return {"value": "", "status": "empty", "evidence": []}

    segments_by_id = {
        str(segment.get("id")): segment
        for segment in api3_segments
        if isinstance(segment, dict) and segment.get("id") is not None
    }
    evidence_values: list[dict[str, Any]] = []
    support_by_category: dict[str, list[str]] = {}
    validation_reasons: list[str] = []
    needs_review = not text or not items
    if needs_review:
        validation_reasons.append("text_and_items_must_coexist")

    rendered_categories: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            validation_reasons.append("category_prefix_missing")
            continue
        category, content = line.split(":", 1)
        rendered_categories[category.strip()] = content.strip()

    for item in items:
        category = str(item.get("category") or "").strip()
        assertion = str(item.get("assertion") or "").strip().upper()
        plan_status = str(item.get("plan_status") or "").strip().upper()
        fact_type = str(item.get("fact_type") or "UNKNOWN").strip().upper()
        action_atoms = [
            atom
            for atom in _atomic_values(item.get("action"))
            if str(atom.get("raw_value") or "").strip()
        ]
        if category not in _TREATMENT_PLAN_CATEGORIES:
            validation_reasons.append("unsupported_category")
        if category and category not in rendered_categories:
            validation_reasons.append("category_not_rendered")
        if _TREATMENT_PLAN_STATUS_ASSERTION.get(plan_status) != assertion:
            validation_reasons.append("plan_status_assertion_mismatch")
        if fact_type != "PLAN":
            validation_reasons.append("unresolved_fact_type")
        if not action_atoms:
            validation_reasons.append("plan_without_evidence")
            continue
        for atom in action_atoms:
            evidence = _evidence_with_segment_id(
                atom.get("evidence"), api3_segments
            )
            if evidence is not None and evidence not in evidence_values:
                evidence_values.append(evidence)
            segment = (
                segments_by_id.get(str(evidence.get("segment_id")))
                if evidence is not None
                else None
            )
            raw_value = str(atom.get("raw_value") or "").strip()
            supports = support_by_category.setdefault(category, [])
            if raw_value:
                supports.append(raw_value)
            if segment is not None:
                supports.extend(
                    str(segment.get(key) or "").strip()
                    for key in (
                        "raw_text",
                        "corrected_text",
                        "translated_text_en",
                    )
                    if str(segment.get(key) or "").strip()
                )
            context = " ".join(supports)
            if segment is None or not _TREATMENT_PLAN_CONTEXT_RE.search(context):
                validation_reasons.append("clinician_plan_context_missing")

            if (
                atom.get("status") in {
                    "needs_confirmation",
                    "asked_but_unanswered",
                }
                and assertion != "UNCERTAIN"
            ):
                validation_reasons.append("assertion_mismatch")
            if plan_status == "CONDITIONAL" or assertion == "UNCERTAIN":
                needs_review = True

    for category, content in rendered_categories.items():
        if category not in _TREATMENT_PLAN_CATEGORIES:
            validation_reasons.append("unsupported_rendered_category")
            continue
        actions = [
            action.strip()
            for action in content.split(",")
            if action.strip()
        ]
        supports = support_by_category.get(category, [])
        if not actions or any(
            not _plan_display_action_supported(action, supports)
            for action in actions
        ):
            validation_reasons.append("unsupported_display_action")

    unique_reasons = list(dict.fromkeys(validation_reasons))
    needs_review = needs_review or bool(unique_reasons)
    return {
        "value": text,
        "status": "needs_review" if needs_review else "filled",
        "evidence": evidence_values,
        **({"_validation_reasons": unique_reasons} if unique_reasons else {}),
    }


def _is_laterality_only(value: str) -> bool:
    normalized = re.sub(r"[\s.,;:()]+", " ", value.casefold()).strip()
    korean = re.sub(r"\s+", "", normalized)
    korean = re.sub(r"(?:에서|으로|에게|이|가|은|는|을|를|에)$", "", korean)
    if korean in {"오른쪽", "우측", "왼쪽", "좌측", "양쪽", "양측"}:
        return True
    return bool(
        re.fullmatch(
            r"(?:right|left)(?:[- ]sided| side)?|bilateral(?:ly)?|both sides?|rt\.?|lt\.?",
            normalized,
            re.IGNORECASE,
        )
    )


def _is_english_location_value(value: str) -> bool:
    return bool(re.search(r"[A-Za-z]", value)) and not bool(
        re.search(r"[ㄱ-ㅎㅏ-ㅣ가-힣]", value)
    )


def _nrs_draft_field(
    pain_assessment: Any,
    api3_segments: list[dict[str, Any]],
) -> dict[str, Any]:
    nrs = pain_assessment.get("nrs") if isinstance(pain_assessment, dict) else None
    location = (
        pain_assessment.get("location")
        if isinstance(pain_assessment, dict)
        else None
    )
    presence = (
        pain_assessment.get("presence")
        if isinstance(pain_assessment, dict)
        else None
    )
    nrs_atoms = list(_atomic_values(nrs))
    location_atoms = list(_atomic_values(location))
    presence_atoms = [
        atom
        for atom in _atomic_values(presence)
        if str(atom.get("raw_value") or "").strip()
    ]
    evidence: list[dict[str, Any]] = []
    for atom in (*nrs_atoms, *location_atoms, *presence_atoms):
        normalized = _evidence_with_segment_id(atom.get("evidence"), api3_segments)
        if normalized is not None and normalized not in evidence:
            evidence.append(normalized)

    score = ""
    for atom in nrs_atoms:
        raw_value = str(atom.get("raw_value") or "").strip()
        match = re.search(
            r"(?<!\d)(10(?:\.0+)?|[0-9](?:\.\d+)?)(?!\d)",
            raw_value,
        )
        if match:
            score = match.group(1).removesuffix(".0")
            break

    location_values: list[str] = []
    seen_locations: set[str] = set()
    segments = {
        str(segment.get("id")): segment
        for segment in api3_segments
        if isinstance(segment, dict) and segment.get("id") is not None
    }
    for atom in location_atoms:
        raw_value = str(atom.get("raw_value") or "").strip()
        if not raw_value or _is_laterality_only(raw_value):
            continue
        normalized_evidence = _evidence_with_segment_id(
            atom.get("evidence"), api3_segments
        )
        segment = (
            segments.get(str(normalized_evidence.get("segment_id")))
            if normalized_evidence
            else None
        )
        display_value = ""
        if segment is not None:
            for annotation in segment.get("annotations", []):
                if not isinstance(annotation, dict):
                    continue
                source_text = str(
                    (annotation.get("source_span") or {}).get("text") or ""
                ).strip()
                if source_text and not (
                    source_text.casefold() in raw_value.casefold()
                    or raw_value.casefold() in source_text.casefold()
                ):
                    continue
                candidates = filter_candidates_for_field(
                    "pain_assessment",
                    annotation.get("candidates", []),
                    annotation_term_type=annotation.get("term_type"),
                )
                is_anatomy = annotation.get("term_type") == "anatomy" or any(
                    candidate.get("collection") == "anatomy_terms"
                    for candidate in candidates
                )
                if not is_anatomy:
                    continue
                display_value = next(
                    (
                        str(term).strip()
                        for term in annotation.get("search_terms_en", [])
                        if str(term).strip()
                    ),
                    "",
                )
                if display_value and (
                    not _is_english_location_value(display_value)
                    or _is_laterality_only(display_value)
                ):
                    display_value = ""
                if not display_value:
                    display_value = next(
                        (
                            str(candidate.get("canonical_en") or "").strip()
                            for candidate in candidates
                            if str(candidate.get("canonical_en") or "").strip()
                        ),
                        "",
                    )
                if display_value and (
                    not _is_english_location_value(display_value)
                    or _is_laterality_only(display_value)
                ):
                    display_value = ""
                if display_value:
                    break
        if not display_value and _is_english_location_value(raw_value):
            display_value = raw_value
        if not display_value:
            continue
        display_value = display_value[:1].upper() + display_value[1:]
        identity = display_value.casefold()
        if identity not in seen_locations:
            seen_locations.add(identity)
            location_values.append(display_value)

    presence_text = " ".join(
        str(atom.get("raw_value") or "").strip() for atom in presence_atoms
    )
    pain_denied = bool(
        re.search(
            r"(?:통증(?:은|이)?\s*(?:없|없음)|안\s*아파|아프지\s*않|"
            r"\bno\s+pain\b|\bden(?:y|ies|ied)\s+pain\b|\bnot\s+in\s+pain\b)",
            presence_text,
            re.IGNORECASE,
        )
    )
    if pain_denied and not score and not location_values:
        return {"value": "통증 없음", "status": "filled", "evidence": evidence}
    if not score and not location_values and not presence_atoms:
        return {"value": "", "status": "empty", "evidence": []}
    return {
        "value": f"NRS {score or '-'} / {', '.join(location_values) or '-'}",
        "status": (
            "needs_review"
            if any(atom.get("status") == "needs_confirmation" for atom in presence_atoms)
            else "filled"
        ),
        "evidence": evidence,
    }


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
            elif field_id == "history":
                fields[field_id] = _hpi_draft_field(
                    record.get(source_key),
                    api3_segments,
                    candidate_decisions=candidate_decisions,
                )
            elif field_id == "past-history":
                fields[field_id] = _past_history_draft_field(
                    record.get(source_key),
                    api3_segments,
                    candidate_decisions=candidate_decisions,
                )
            elif field_id == "medication":
                fields[field_id] = _medications_draft_field(
                    record.get(source_key),
                    api3_segments,
                    candidate_decisions=candidate_decisions,
                )
            elif field_id == "allergy":
                fields[field_id] = _allergy_draft_field(
                    record.get(source_key),
                    api3_segments,
                    candidate_decisions=candidate_decisions,
                )
            elif field_id == "social":
                fields[field_id] = _social_history_draft_field(
                    record.get(source_key),
                    api3_segments,
                    candidate_decisions=candidate_decisions,
                )
            elif field_id == "physical":
                fields[field_id] = _physical_exam_draft_field(
                    record.get(source_key),
                    api3_segments,
                    candidate_decisions=candidate_decisions,
                )
            elif field_id == "treatment-plan":
                fields[field_id] = _treatment_plan_draft_field(
                    record.get(source_key),
                    api3_segments,
                    candidate_decisions=candidate_decisions,
                )
            elif field_id == "impression":
                fields[field_id] = _impression_draft_field(
                    record.get(source_key),
                    api3_segments,
                    candidate_decisions=candidate_decisions,
                )
            elif field_id == "outcome":
                fields[field_id] = _outcome_draft_field(
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
    physical_validation_reasons = fields["physical"].pop(
        "_validation_reasons", []
    )
    if physical_validation_reasons:
        review_items.append(
            {
                "id": "physical-examination:validation",
                "type": "physical_examination_validation",
                "field_id": "physical",
                "segment_id": (
                    fields["physical"].get("evidence", [{}])[0].get("segment_id")
                    if fields["physical"].get("evidence")
                    else None
                ),
                "source": fields["physical"].get("value", ""),
                "evidence": fields["physical"].get("value", ""),
                "candidates": [],
                "validation_reasons": physical_validation_reasons,
                "needs_review": True,
            }
        )
    treatment_plan_validation_reasons = fields["treatment-plan"].pop(
        "_validation_reasons", []
    )
    if treatment_plan_validation_reasons:
        review_items.append(
            {
                "id": "treatment-plan:validation",
                "type": "treatment_plan_validation",
                "field_id": "treatment-plan",
                "segment_id": (
                    fields["treatment-plan"].get("evidence", [{}])[0].get(
                        "segment_id"
                    )
                    if fields["treatment-plan"].get("evidence")
                    else None
                ),
                "source": fields["treatment-plan"].get("value", ""),
                "evidence": fields["treatment-plan"].get("value", ""),
                "candidates": [],
                "validation_reasons": treatment_plan_validation_reasons,
                "needs_review": True,
            }
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
        "dictionary_version",
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
            "dictionary_version": match.dictionary_version,
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


def _legacy_field_evidence_segments(field: Any) -> set[str]:
    if not isinstance(field, dict):
        return set()
    return {
        str(item.get("segment_id"))
        for item in field.get("evidence", [])
        if isinstance(item, dict) and item.get("segment_id") is not None
    }


def _compact_field_evidence_segments(
    field: Any,
    facts: dict[str, Any],
) -> set[str]:
    if not isinstance(field, dict):
        return set()
    segments: set[str] = set()
    for fact_ref in field.get("fact_refs", []):
        fact = facts.get(str(fact_ref))
        if not isinstance(fact, dict):
            continue
        segments.update(
            str(segment_id)
            for segment_id in fact.get("segments", [])
            if isinstance(segment_id, str)
        )
    return segments


def _comparison_text(value: Any, *, mentioned: bool) -> str:
    if not mentioned:
        return ""
    return " ".join(str(value or "").split()).casefold()


def run_clinical_workflow(
    whisper_payload: dict[str, Any],
    *,
    retriever: Any | None,
    clinical_extractor: Any,
    query_expander: Any | None = None,
    medical_query_resolver: Any | None = None,
    preserve_unsupported: bool = False,
    include_query_resolution_summary: bool = False,
    compact_v3_mode: str = "off",
) -> dict[str, Any]:
    from .contracts import validate_whisper_payload
    from .candidate_snapshot import snapshots_from_api3_document
    from .compact_primary import candidate_field_routes, project_compact_primary_draft
    from .query_expansion import run_query_expansion

    compact_v3_mode = str(compact_v3_mode or "off").strip().casefold()
    allowed_compact_modes = {
        "off",
        "compare",
        "primary",
        "legacy",
        "lean_shadow",
        "lean_primary",
    }
    if compact_v3_mode not in allowed_compact_modes:
        raise ValueError(
            "compact_v3_mode must be off, compare, primary, legacy, "
            "lean_shadow, or lean_primary"
        )
    compact_v3_primary = compact_v3_mode in {
        "primary",
        "legacy",
        "lean_shadow",
        "lean_primary",
    }
    lean_primary = compact_v3_mode == "lean_primary"

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

    def telemetry_counts(value: object) -> dict[str, int]:
        if not isinstance(value, dict):
            return {}
        return {
            str(key): telemetry_count(count)
            for key, count in value.items()
            if isinstance(key, str) and telemetry_count(count) > 0
        }

    def translation_batch_details(
        value: object,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        details: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            details.append(
                {
                    "batch_index": telemetry_count(item.get("batch_index")),
                    "target_segment_count": telemetry_count(
                        item.get("target_segment_count")
                    ),
                    "context_segment_count": telemetry_count(
                        item.get("context_segment_count")
                    ),
                    "request_count": telemetry_count(item.get("request_count")),
                    "retry_split_count": telemetry_count(
                        item.get("retry_split_count")
                    ),
                    "partial_retry_count": telemetry_count(
                        item.get("partial_retry_count")
                    ),
                    "preserved_segment_count": telemetry_count(
                        item.get("preserved_segment_count")
                    ),
                    "retry_reasons": telemetry_counts(
                        item.get("retry_reasons")
                    ),
                    "rate_limit_count": telemetry_count(
                        item.get("rate_limit_count")
                    ),
                    "failed_segment_count": telemetry_count(
                        item.get("failed_segment_count")
                    ),
                    "elapsed_ms": telemetry_number(item.get("elapsed_ms")),
                }
            )
        return details

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

    def run_record_stage(
        record_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], float]:
        started = time.perf_counter()
        extracted = staged_extract_record(record_payload)
        return extracted, round((time.perf_counter() - started) * 1000, 3)

    query_expansion, measured_translation_ms = run_translation_stage()
    if staged_extraction and not compact_v3_primary:
        translations = {
            str(item.get("segment_id")): str(
                item.get("translated_text_en") or ""
            ).strip()
            for item in query_expansion.get("translated_segments", [])
            if isinstance(item, dict)
            and item.get("segment_id") is not None
            and str(item.get("translated_text_en") or "").strip()
        }
        record_payload = {
            **whisper_payload,
            "segments": [
                {
                    **segment,
                    **(
                        {"translated_text_en": translations[str(segment.get("id"))]}
                        if str(segment.get("id")) in translations
                        else {}
                    ),
                }
                for segment in whisper_payload.get("segments", [])
                if isinstance(segment, dict)
            ],
        }
        extracted_record_stage, clinical_record_stage_ms = run_record_stage(
            record_payload
        )
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
        "translation_batch_count": telemetry_count(
            query_expansion_telemetry.get("translation_batch_count", 0)
        ),
        "translation_worker_count": telemetry_count(
            query_expansion_telemetry.get("translation_worker_count", 0)
        ),
        "translation_retry_split_count": telemetry_count(
            query_expansion_telemetry.get("translation_retry_split_count", 0)
        ),
        "translation_partial_retry_count": telemetry_count(
            query_expansion_telemetry.get("translation_partial_retry_count", 0)
        ),
        "translation_preserved_segment_count": telemetry_count(
            query_expansion_telemetry.get(
                "translation_preserved_segment_count",
                0,
            )
        ),
        "translation_retry_reasons": telemetry_counts(
            query_expansion_telemetry.get("translation_retry_reasons")
        ),
        "translation_rate_limit_count": telemetry_count(
            query_expansion_telemetry.get("translation_rate_limit_count", 0)
        ),
        "translation_batches": translation_batch_details(
            query_expansion_telemetry.get("translation_batches")
        ),
        "translation_provider_calls": telemetry_count(
            query_expansion_telemetry.get("translation_provider_calls", 0)
        ),
        "translation_network_retries": telemetry_count(
            query_expansion_telemetry.get("translation_network_retries", 0)
        ),
        "translation_http_ms": telemetry_number(
            query_expansion_telemetry.get("translation_http_ms", 0.0)
        ),
        "translation_provider_ms": telemetry_number(
            query_expansion_telemetry.get("translation_provider_ms", 0.0)
        ),
        "translation_provider_load_ms": telemetry_number(
            query_expansion_telemetry.get("translation_provider_load_ms", 0.0)
        ),
        "translation_prompt_eval_ms": telemetry_number(
            query_expansion_telemetry.get("translation_prompt_eval_ms", 0.0)
        ),
        "translation_token_eval_ms": telemetry_number(
            query_expansion_telemetry.get("translation_token_eval_ms", 0.0)
        ),
        "translation_unattributed_http_ms": telemetry_number(
            query_expansion_telemetry.get(
                "translation_unattributed_http_ms",
                0.0,
            )
        ),
        "umls_ms": 0.0,
        "umls_model_load_ms": 0.0,
        "umls_mention_detection_ms": 0.0,
        "umls_linking_ms": 0.0,
        "umls_extraction_ms": 0.0,
        "umls_worker_overhead_ms": 0.0,
        "umls_worker_cold_start_overhead_ms": 0.0,
        "umls_worker_batch_count": 0,
        "umls_worker_fallback_batch_count": 0,
        "umls_worker_cold_start_batch_count": 0,
        "umls_input_segment_count": 0,
        "umls_input_character_count": 0,
        "umls_detected_span_count": 0,
        "umls_detected_span_character_count": 0,
        "umls_linker_document_count": 0,
        "dictionary_ms": 0.0,
        "vector_ms": 0.0,
        "exact_statement_count": 0,
        "vector_statement_count": 0,
        "search_cache_hit_count": 0,
        "exact_search_batch_count": 0,
        "exact_search_query_count": 0,
        "exact_search_hit_count": 0,
        "vector_fallback_batch_count": 0,
        "vector_fallback_query_count": 0,
        "vector_fallback_hit_count": 0,
        "vector_fallback_empty_count": 0,
        "umls_surface_query_count": 0,
        "umls_canonical_query_count": 0,
        "semantic_fallback_query_count": 0,
        "ngram_fallback_query_count": 0,
        "vector_drug_terms_ms": 0.0,
        "vector_drug_terms_statement_count": 0,
        "vector_drug_terms_batch_count": 0,
        "vector_drug_terms_query_count": 0,
        "vector_drug_terms_candidate_count": 0,
        "vector_drug_terms_empty_query_count": 0,
        "vector_drug_terms_ingredient_ms": 0.0,
        "vector_drug_terms_ingredient_result_count": 0,
        "vector_drug_terms_product_ms": 0.0,
        "vector_drug_terms_product_result_count": 0,
        "vector_procedure_terms_ms": 0.0,
        "vector_procedure_terms_statement_count": 0,
        "vector_procedure_terms_batch_count": 0,
        "vector_procedure_terms_query_count": 0,
        "vector_procedure_terms_candidate_count": 0,
        "vector_procedure_terms_empty_query_count": 0,
        "vector_anatomy_terms_ms": 0.0,
        "vector_anatomy_terms_statement_count": 0,
        "vector_anatomy_terms_batch_count": 0,
        "vector_anatomy_terms_query_count": 0,
        "vector_anatomy_terms_candidate_count": 0,
        "vector_anatomy_terms_empty_query_count": 0,
        "vector_emergency_terms_ms": 0.0,
        "vector_emergency_terms_statement_count": 0,
        "vector_emergency_terms_batch_count": 0,
        "vector_emergency_terms_query_count": 0,
        "vector_emergency_terms_candidate_count": 0,
        "vector_emergency_terms_empty_query_count": 0,
        "clinical_extraction_ms": 0.0,
        "clinical_llm_fact_chunk_count": 0,
        "clinical_llm_fact_chunk_worker_count": 0,
        "clinical_llm_field_group_call_count": 0,
        "clinical_llm_length_fallback_count": 0,
        "clinical_llm_repair_count": 0,
        "clinical_llm_regeneration_count": 0,
        "clinical_llm_validation_failure_reasons": [],
        "clinical_llm_failed_segment_count": 0,
        "clinical_llm_provider_calls": 0,
        "clinical_llm_network_retries": 0,
        "clinical_llm_http_ms": 0.0,
        "clinical_llm_provider_ms": 0.0,
        "clinical_llm_provider_load_ms": 0.0,
        "clinical_llm_prompt_eval_ms": 0.0,
        "clinical_llm_token_eval_ms": 0.0,
        "clinical_llm_unattributed_http_ms": 0.0,
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
                for name in (
                    "umls_model_load_ms",
                    "umls_mention_detection_ms",
                    "umls_linking_ms",
                    "umls_extraction_ms",
                    "umls_worker_overhead_ms",
                    "umls_worker_cold_start_overhead_ms",
                ):
                    telemetry[name] = telemetry_number(
                        getattr(resolution_telemetry, name, 0.0)
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
                for name in (
                    "umls_worker_batch_count",
                    "umls_worker_fallback_batch_count",
                    "umls_worker_cold_start_batch_count",
                    "umls_input_segment_count",
                    "umls_input_character_count",
                    "umls_detected_span_count",
                    "umls_detected_span_character_count",
                    "umls_linker_document_count",
                    "exact_search_batch_count",
                    "exact_search_query_count",
                    "exact_search_hit_count",
                    "vector_fallback_batch_count",
                    "vector_fallback_query_count",
                    "vector_fallback_hit_count",
                    "vector_fallback_empty_count",
                    "umls_surface_query_count",
                    "umls_canonical_query_count",
                    "semantic_fallback_query_count",
                    "ngram_fallback_query_count",
                ):
                    telemetry[name] = telemetry_count(
                        getattr(resolution_telemetry, name, 0)
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
                collection_batch_counts = dict(
                    getattr(
                        resolution_telemetry,
                        "vector_collection_batch_counts",
                        (),
                    )
                )
                collection_query_counts = dict(
                    getattr(
                        resolution_telemetry,
                        "vector_collection_query_counts",
                        (),
                    )
                )
                collection_candidate_counts = dict(
                    getattr(
                        resolution_telemetry,
                        "vector_collection_candidate_counts",
                        (),
                    )
                )
                collection_empty_query_counts = dict(
                    getattr(
                        resolution_telemetry,
                        "vector_collection_empty_query_counts",
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
                    for metric_name, values in (
                        ("batch_count", collection_batch_counts),
                        ("query_count", collection_query_counts),
                        ("candidate_count", collection_candidate_counts),
                        ("empty_query_count", collection_empty_query_counts),
                    ):
                        telemetry[f"vector_{collection}_{metric_name}"] = (
                            telemetry_count(values.get(collection, 0))
                        )
                partition_ms = {
                    (collection, partition): elapsed_ms
                    for collection, partition, elapsed_ms in getattr(
                        resolution_telemetry,
                        "vector_partition_ms",
                        (),
                    )
                }
                partition_result_counts = {
                    (collection, partition): count
                    for collection, partition, count in getattr(
                        resolution_telemetry,
                        "vector_partition_result_counts",
                        (),
                    )
                }
                for partition in ("ingredient", "product"):
                    telemetry[f"vector_drug_terms_{partition}_ms"] = (
                        telemetry_number(
                            partition_ms.get(("drug_terms", partition), 0.0)
                        )
                    )
                    telemetry[
                        f"vector_drug_terms_{partition}_result_count"
                    ] = telemetry_count(
                        partition_result_counts.get(
                            ("drug_terms", partition),
                            0,
                        )
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
    api2_payload: dict[str, Any] | None = None
    compact_primary_result: dict[str, Any] | None = None
    compact_primary_snapshots: dict[str, dict[str, Any]] = {}
    clinical_extraction_started = time.perf_counter()
    try:
        api2_payload = _api2_payload(
            whisper_payload,
            api3_document,
            query_expansion,
        )
        if compact_v3_primary:
            generate_compact = getattr(
                clinical_extractor,
                (
                    "generate_compact_record_lean"
                    if lean_primary
                    else "generate_compact_record"
                ),
                None,
            )
            if not callable(generate_compact) and not lean_primary:
                generate_compact = getattr(
                    clinical_extractor,
                    "compare_compact_record",
                    None,
                )
            if not callable(generate_compact):
                raise RuntimeError("Compact primary generation is unavailable")
            metadata = (
                api3_document.get("metadata")
                if isinstance(api3_document.get("metadata"), dict)
                else {}
            )
            created_at = metadata.get("created_at")
            compact_primary_snapshots = snapshots_from_api3_document(
                api3_document,
                request_id=f"local-primary:{created_at or 'undated'}",
                created_at=created_at if isinstance(created_at, str) else None,
            )
            compact_primary_result = generate_compact(
                api2_payload,
                compact_primary_snapshots,
            )
            if not isinstance(compact_primary_result, dict):
                raise ValueError("Compact v3 generator returned an invalid contract")
            compact_generation = compact_primary_result.get("generation")
            if isinstance(compact_generation, dict):
                for key, value in compact_generation.items():
                    telemetry[key] = value
            compact_record = compact_primary_result.get("record")
            compact_validation = compact_primary_result.get("validation")
            if not isinstance(compact_record, dict) or not isinstance(
                compact_validation, dict
            ):
                raise ValueError("Compact v3 generator returned an invalid contract")
            api2_document = {
                "schema_version": "clinical-record-v2",
                "clinical_record": {},
                "unresolved_questions": [],
                "validation_warnings": [],
                "candidate_decisions": [],
                "draft_suggestions": [],
                "metadata": {
                    "model": getattr(clinical_extractor, "model_name", None),
                    "prompt_version": compact_primary_result.get("prompt_version"),
                    "candidate_prompt_version": None,
                    "draft_normalization_prompt_version": None,
                },
            }
        elif staged_extraction and extracted_record_stage is not None:
            api2_document = staged_finalize_record(
                extracted_record_stage,
                api2_payload,
            )
        else:
            api2_document = clinical_extractor.extract(api2_payload)
        if not isinstance(api2_document, dict):
            raise ValueError("clinical extractor returned an invalid contract")
        if not compact_v3_primary:
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
                translated_segments=query_expansion.get("translated_segments"),
            )
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

    generation_telemetry = (
        compact_primary_result.get("generation")
        if isinstance(compact_primary_result, dict)
        and isinstance(compact_primary_result.get("generation"), dict)
        else {}
    )
    telemetry["clinical_llm_provider_calls"] = telemetry_count(
        generation_telemetry.get("provider_call_count", 0)
    )
    for target_key, source_key in (
        ("clinical_llm_fact_chunk_count", "fact_chunk_count"),
        ("clinical_llm_fact_chunk_worker_count", "fact_chunk_worker_count"),
        ("clinical_llm_field_group_call_count", "field_group_call_count"),
        ("clinical_llm_length_fallback_count", "length_fallback_count"),
        ("clinical_llm_repair_count", "repair_count"),
        ("clinical_llm_regeneration_count", "regeneration_count"),
        ("clinical_llm_failed_segment_count", "failed_segment_count"),
    ):
        telemetry[target_key] = telemetry_count(
            generation_telemetry.get(source_key, 0)
        )
    telemetry["clinical_llm_network_retries"] = telemetry_count(
        generation_telemetry.get("network_retry_count", 0)
    )
    validation_failure_reasons = generation_telemetry.get(
        "validation_failure_reasons", []
    )
    telemetry["clinical_llm_validation_failure_reasons"] = (
        [str(reason) for reason in validation_failure_reasons]
        if isinstance(validation_failure_reasons, list)
        else []
    )
    for target_key, source_key in (
        ("clinical_llm_http_ms", "http_elapsed_ms"),
        ("clinical_llm_provider_ms", "provider_total_ms"),
        ("clinical_llm_provider_load_ms", "provider_load_ms"),
        ("clinical_llm_prompt_eval_ms", "provider_prompt_eval_ms"),
        ("clinical_llm_token_eval_ms", "provider_eval_ms"),
        ("clinical_llm_unattributed_http_ms", "unattributed_http_ms"),
    ):
        telemetry[target_key] = telemetry_number(
            generation_telemetry.get(source_key, 0.0)
        )

    api3_status = (api3_document.get("metadata") or {}).get("processing_status")
    processing_status = (
        "partial" if errors or api3_status == "partial" else "completed"
    )
    if compact_primary_result is not None:
        compact_processing_status = str(
            (compact_primary_result.get("validation") or {}).get(
                "processing_status", "completed"
            )
        )
        if compact_processing_status in {"partial", "failed"}:
            processing_status = compact_processing_status
    candidate_decisions = _candidate_decisions(api2_document, api3_document)
    if api2_document is not None:
        api2_document["candidate_decisions"] = _api2_candidate_decisions(
            candidate_decisions
        )
    audit = _workflow_audit(api3_document, api2_document)
    translated_segments = (
        query_expansion.get("translated_segments")
        if isinstance(query_expansion.get("translated_segments"), list)
        else None
    )
    if compact_primary_result is not None:
        draft = project_compact_primary_draft(
            compact_primary_result["record"],
            compact_primary_result["validation"],
            api3_document,
            translated_segments,
        )
        candidate_reviews = _review_items(
            api3_document,
            candidate_decisions,
            {},
        )
        routes = candidate_field_routes(
            compact_primary_result["record"],
            compact_primary_snapshots,
        )
        for item in candidate_reviews:
            route = routes.get(
                (str(item.get("segment_id") or ""), int(str(item["id"]).rsplit(":", 1)[-1]))
            ) if str(item.get("id") or "").rsplit(":", 1)[-1].isdigit() else None
            if route is not None:
                item["field_id"] = route
            draft["review_items"].append(item)
            field = draft["fields"].get(str(item.get("field_id") or ""))
            if isinstance(field, dict):
                field["status"] = "needs_review"
                field["suggestion_status"] = "UNRESOLVED"
    else:
        draft = build_draft(
            api2_document,
            api3_document,
            candidate_decisions,
            translated_segments,
        )
    compact_comparison: dict[str, Any] | None = None
    if compact_v3_mode in {"compare", "lean_shadow"}:
        started = time.perf_counter()
        compare = getattr(
            clinical_extractor,
            (
                "generate_compact_record_lean"
                if compact_v3_mode == "lean_shadow"
                else "compare_compact_record"
            ),
            None,
        )
        try:
            if not callable(compare) or api2_payload is None:
                raise RuntimeError("Compact v3 comparison is unavailable")
            metadata = (
                api3_document.get("metadata")
                if isinstance(api3_document.get("metadata"), dict)
                else {}
            )
            created_at = metadata.get("created_at")
            request_id = f"local-compare:{created_at or 'undated'}"
            snapshots = snapshots_from_api3_document(
                api3_document,
                request_id=request_id,
                created_at=created_at if isinstance(created_at, str) else None,
            )
            compact_result = compare(api2_payload, snapshots)
            compact_record = (
                compact_result.get("record")
                if isinstance(compact_result, dict)
                and isinstance(compact_result.get("record"), dict)
                else {}
            )
            compact_fields = (
                compact_record.get("fields")
                if isinstance(compact_record.get("fields"), dict)
                else {}
            )
            compact_facts = (
                compact_record.get("facts")
                if isinstance(compact_record.get("facts"), dict)
                else {}
            )
            legacy_fields = (
                draft.get("fields") if isinstance(draft.get("fields"), dict) else {}
            )
            field_comparison: dict[str, Any] = {}
            mismatch_field_ids: list[str] = []
            evidence_mismatch_field_ids: list[str] = []
            for canonical_id, legacy_id in _CANONICAL_TO_LEGACY_FIELD_ID.items():
                legacy_field = legacy_fields.get(legacy_id)
                compact_field = compact_fields.get(canonical_id)
                v2_text = (
                    legacy_field.get("value")
                    if isinstance(legacy_field, dict)
                    else None
                )
                v3_text = (
                    compact_field.get("text")
                    if isinstance(compact_field, dict)
                    else None
                )
                v2_mentioned = (
                    isinstance(legacy_field, dict)
                    and legacy_field.get("status") not in {"empty", "unknown"}
                    and bool(str(v2_text or "").strip())
                )
                v3_mentioned = (
                    isinstance(compact_field, dict)
                    and bool(str(v3_text or "").strip())
                    and bool(compact_field.get("fact_refs"))
                    and compact_field.get("generation_status")
                    in {None, "GENERATED"}
                )
                normalized_v2 = _comparison_text(
                    v2_text,
                    mentioned=v2_mentioned,
                )
                normalized_v3 = _comparison_text(
                    v3_text,
                    mentioned=v3_mentioned,
                )
                matches = normalized_v2 == normalized_v3
                v2_segments = _legacy_field_evidence_segments(legacy_field)
                v3_segments = _compact_field_evidence_segments(
                    compact_field,
                    compact_facts,
                )
                same_evidence = v2_segments == v3_segments
                shared_evidence = bool(v2_segments & v3_segments)
                if matches:
                    comparison_class = "EXACT_MATCH"
                elif v2_mentioned != v3_mentioned:
                    comparison_class = "MISSINGNESS_MISMATCH"
                elif same_evidence and v2_segments:
                    comparison_class = "TEXT_VARIANT_SAME_EVIDENCE"
                elif shared_evidence:
                    comparison_class = "TEXT_VARIANT_SHARED_EVIDENCE"
                elif not v2_segments and not v3_segments:
                    comparison_class = "TEXT_VARIANT_NO_EVIDENCE"
                else:
                    comparison_class = "EVIDENCE_MISMATCH"
                if not matches:
                    mismatch_field_ids.append(canonical_id)
                if comparison_class in {
                    "MISSINGNESS_MISMATCH",
                    "EVIDENCE_MISMATCH",
                }:
                    evidence_mismatch_field_ids.append(canonical_id)
                field_comparison[canonical_id] = {
                    "v2_text": v2_text,
                    "compact_v3_text": v3_text,
                    "matches": matches,
                    "comparison_class": comparison_class,
                    "v2_segment_ids": sorted(v2_segments),
                    "compact_v3_segment_ids": sorted(v3_segments),
                }
            compact_comparison = {
                "schema_version": "clinical-record-compact-comparison-v1",
                "status": "completed",
                "prompt_version": compact_result.get("prompt_version"),
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "candidate_snapshot_count": len(snapshots),
                "record": compact_record,
                "validation": compact_result.get("validation"),
                "fields": field_comparison,
                "mismatch_field_ids": mismatch_field_ids,
                "evidence_mismatch_field_ids": evidence_mismatch_field_ids,
            }
            if compact_v3_mode == "compare":
                compact_comparison["candidate_snapshots"] = list(snapshots.values())
            if isinstance(compact_result.get("generation"), dict):
                compact_comparison["generation"] = compact_result["generation"]
        except Exception as error:
            compact_comparison = {
                "schema_version": "clinical-record-compact-comparison-v1",
                "status": "unavailable",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "error_code": type(error).__name__,
                "detail": (
                    "Compact v3 comparison failed; the authoritative v2 draft "
                    "was preserved"
                ),
            }
    compact_primary_output: dict[str, Any] | None = None
    if compact_primary_result is not None:
        compact_primary_output = {
            "schema_version": "clinical-record-compact-primary-v1",
            "status": str(
                (compact_primary_result.get("validation") or {}).get(
                    "processing_status", "completed"
                )
            ),
            "prompt_version": compact_primary_result.get("prompt_version"),
            "elapsed_ms": telemetry.get("clinical_extraction_ms", 0.0),
            "candidate_snapshot_count": len(compact_primary_snapshots),
            "record": compact_primary_result.get("record"),
            "validation": compact_primary_result.get("validation"),
        }
        if not lean_primary:
            compact_primary_output["candidate_snapshots"] = list(
                compact_primary_snapshots.values()
            )
        if isinstance(compact_primary_result.get("generation"), dict):
            compact_primary_output["generation"] = compact_primary_result["generation"]
        if isinstance(compact_primary_result.get("audit"), dict):
            compact_primary_output["generation_audit"] = compact_primary_result["audit"]
        audit["references"]["compact_record_path"] = "$.compact_v3_primary.record"
        audit["versions"]["compact_prompt"] = compact_primary_result.get(
            "prompt_version"
        )
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
        "draft": draft,
        "errors": errors,
        "telemetry": {
            key: round(value, 3) if isinstance(value, float) else value
            for key, value in telemetry.items()
        },
    }
    if include_query_resolution_summary and query_resolution_summary is not None:
        result["query_resolution"] = query_resolution_summary
    if compact_comparison is not None:
        result["compact_v3_comparison"] = compact_comparison
    if compact_primary_output is not None:
        result["compact_v3_primary"] = compact_primary_output
    return result

