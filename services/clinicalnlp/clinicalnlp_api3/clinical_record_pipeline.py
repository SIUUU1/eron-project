from __future__ import annotations

import copy
from datetime import datetime, timezone
import re
from typing import Any


STATUSES = {
    "confirmed",
    "not_mentioned",
    "asked_but_unanswered",
    "needs_confirmation",
}


def select_clinical_record_candidate(
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Select the generated record when a model response contains JSON echoes."""
    if not candidates:
        raise ValueError("no clinical record candidates")

    def confirmed_values(value: Any) -> int:
        if isinstance(value, dict):
            own = int(
                value.get("status") == "confirmed"
                and value.get("raw_value") not in (None, "")
            )
            return own + sum(confirmed_values(item) for item in value.values())
        if isinstance(value, list):
            return sum(confirmed_values(item) for item in value)
        return 0

    return max(
        enumerate(candidates),
        key=lambda pair: (
            confirmed_values(pair[1].get("clinical_record", {})),
            pair[0],
        ),
    )[1]


def _segments(payload: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise ValueError("input must contain a segments array")
    indexed: dict[Any, dict[str, Any]] = {}
    for position, segment in enumerate(segments):
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            raise ValueError("each segment must contain text")
        segment_id = segment.get("id", position)
        if segment_id in indexed:
            raise ValueError(f"duplicate segment id: {segment_id}")
        if not isinstance(segment.get("start"), (int, float)) or not isinstance(
            segment.get("end"), (int, float)
        ):
            raise ValueError(
                f"segment {segment_id} must contain numeric start and end"
            )
        indexed[segment_id] = segment
    return indexed


def _resolve_segment_id(segment_id: Any, segments: dict[Any, Any]) -> Any:
    if segment_id in segments:
        return segment_id
    match = re.fullmatch(r"(.+?)(\d+)", str(segment_id))
    if not match:
        return None
    prefix, number = match.groups()
    candidates = []
    for candidate in segments:
        candidate_match = re.fullmatch(r"(.+?)(\d+)", str(candidate))
        if (
            candidate_match
            and candidate_match.group(1) == prefix
            and int(candidate_match.group(2)) == int(number)
        ):
            candidates.append(candidate)
    return candidates[0] if len(candidates) == 1 else None


def extract_clinical_record(
    payload: dict[str, Any],
    model_response: dict[str, Any],
    *,
    model_name: str,
    prompt_version: str,
) -> dict[str, Any]:
    """Validate and ground the model's structured clinical record."""
    segments = _segments(payload)
    record = (
        model_response.get("clinical_record")
        if isinstance(model_response, dict)
        else None
    )
    if not isinstance(record, dict):
        raise ValueError("model response must contain a clinical_record object")
    default_record = {
        "chief_complaint": [],
        "pain_assessment": {
            key: {
                "raw_value": None,
                "status": "not_mentioned",
                "evidence": None,
            }
            for key in ("presence", "nrs", "location", "quality", "radiation")
        },
        "history_of_present_illness": {
            "text": "",
            **{
                key: {
                    "raw_value": None,
                    "status": "not_mentioned",
                    "evidence": None,
                }
                for key in (
                    "onset",
                    "location",
                    "duration",
                    "character",
                    "course",
                    "radiation",
                    "timing",
                    "severity",
                    "arrival",
                )
            },
            "aggravating_factors": [],
            "alleviating_factors": [],
            "associated_symptoms": [],
            "pre_hospital_care": [],
        },
        "past_history": {
            "text": "",
            "underlying_conditions": [],
            "surgery_history": [],
            "previous_admissions": [],
            **{
                key: {
                    "raw_value": None,
                    "status": "not_mentioned",
                    "evidence": None,
                }
                for key in (
                    "medical_history_status",
                    "surgical_history_status",
                    "admission_history_status",
                )
            },
        },
        "medications": {
            "text": "",
            "items": [],
            "medication_status": {
                "raw_value": None,
                "status": "not_mentioned",
                "evidence": None,
            },
            "last_dose": {
                "raw_value": None,
                "status": "not_mentioned",
                "evidence": None,
            },
        },
        "drug_allergy": {
            "text": "",
            "items": [],
            "allergy_status": {
                "raw_value": None,
                "status": "not_mentioned",
                "evidence": None,
            },
            "specific_denials": [],
        },
        "social_history": {
            "text": "",
            "smoking": {
                "text": "",
                "state": None,
                **{
                    key: {
                        "raw_value": None,
                        "value": None,
                        "status": "not_mentioned",
                        "evidence": None,
                    }
                    for key in (
                        "smoking_status",
                        "packs_per_day",
                        "cigarettes_per_day",
                        "duration_years",
                        "quit_years",
                    )
                },
                "pack_years": None,
                "pack_years_provenance": None,
                "measurement_conflict": False,
            },
            "alcohol": {
                "text": "",
                **{
                    key: {
                        "raw_value": None,
                        "value": None,
                        "status": "not_mentioned",
                        "evidence": None,
                    }
                    for key in (
                        "alcohol_status",
                        "frequency",
                        "type",
                        "amount_per_occasion",
                    )
                },
            },
        },
        "review_of_systems": {
            "text": None,
            "items": [],
        },
        "physical_examination": {
            "text": None,
            "findings": [],
        },
        "impression": {
            "text": None,
            "items": [],
        },
        "treatment_plan": {
            "text": None,
            "items": [],
        },
        "outcome": {
            "text": None,
            "category": None,
            "information_status": "NOT_ASSESSED",
            "decision": {
                "raw_value": None,
                "status": "not_mentioned",
                "evidence": None,
            },
            "detail": {
                "raw_value": None,
                "status": "not_mentioned",
                "evidence": None,
            },
        },
    }
    warnings: list[str] = []

    def with_defaults(value: Any, template: Any) -> Any:
        if isinstance(template, dict):
            if isinstance(value, list) and "status" in template:
                return [with_defaults(item, template) for item in value]
            if isinstance(value, list) and any(
                key in template for key in ("items", "findings")
            ):
                return copy.deepcopy(value)
            if (
                isinstance(value, dict)
                and "status" in value
                and "status" not in template
            ):
                return copy.deepcopy(value)
            source = value if isinstance(value, dict) else {}
            return {
                key: (
                    with_defaults(source[key], item)
                    if key in source
                    else copy.deepcopy(item)
                )
                for key, item in template.items()
            }
        return value if value is not None else copy.deepcopy(template)

    record = with_defaults(record, default_record)

    def evidence_match_score(raw_value: Any, source_text: str) -> float:
        if not isinstance(raw_value, str) or not raw_value.strip():
            return 0.0
        raw_text = re.sub(r"\s+", "", raw_value)
        compact_source = re.sub(r"\s+", "", source_text)
        if raw_text in compact_source or compact_source in raw_text:
            return 1.0
        raw_tokens = {
            token
            for token in re.findall(r"[\w가-힣]+", raw_value)
            if len(token) >= 2
        }
        source_tokens = set(re.findall(r"[\w가-힣]+", source_text))
        if not raw_tokens:
            return 0.0
        return len(raw_tokens & source_tokens) / len(raw_tokens)

    def segment_match_score(raw_value: Any, segment: dict[str, Any]) -> float:
        return max(
            evidence_match_score(raw_value, str(segment.get(key) or ""))
            for key in (
                "text",
                "raw_text",
                "corrected_text",
                "translated_text_en",
            )
        )

    def normalize_evidence(evidence: Any, raw_value: Any = None) -> dict[str, Any]:
        if isinstance(evidence, str):
            reference = evidence
        elif isinstance(evidence, dict):
            if set(evidence) != {"source_segment_id"}:
                raise ValueError(
                    "clinical record evidence must contain only source_segment_id"
                )
            reference = evidence["source_segment_id"]
        else:
            raise ValueError("clinical record value has invalid evidence")
        segment_id = _resolve_segment_id(reference, segments)
        if segment_id is None:
            raise ValueError("clinical record evidence references missing segment")
        source = segments[segment_id]
        if raw_value is not None and segment_match_score(raw_value, source) < 0.5:
            candidates = [
                (
                    segment_match_score(raw_value, candidate),
                    candidate_id,
                    candidate,
                )
                for candidate_id, candidate in segments.items()
            ]
            score, corrected_id, corrected_source = max(
                candidates, key=lambda item: item[0]
            )
            if score >= 0.5:
                warnings.append(
                    "clinical record evidence was corrected to match raw_value"
                )
                segment_id, source = corrected_id, corrected_source
            else:
                warnings.append(
                    "clinical record evidence could not be text-matched to raw_value"
                )
        return {
            "text": source["text"],
            "start": source["start"],
            "end": source["end"],
        }

    def normalize(value: Any) -> Any:
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: normalize(item) for key, item in value.items()}
        if "status" in result:
            status = result["status"]
            if status == "unanswered":
                status = "asked_but_unanswered"
                result["status"] = status
            if status not in STATUSES:
                raise ValueError(f"unsupported clinical record status: {status}")
            if status == "confirmed":
                if not result.get("raw_value"):
                    raise ValueError(
                        "confirmed clinical record value is missing raw_value"
                    )
                result["evidence"] = normalize_evidence(
                    result.get("evidence"), result.get("raw_value")
                )
            elif status in {"not_mentioned", "asked_but_unanswered"}:
                if result.get("evidence") not in (None, {}):
                    warnings.append("non-confirmed value evidence was discarded")
                result["raw_value"] = None
                result["evidence"] = None
            elif status == "needs_confirmation" and result.get("evidence") not in (
                None,
                {},
            ):
                result["evidence"] = normalize_evidence(
                    result["evidence"], result.get("raw_value")
                )
            return result
        if "raw_value" in result and "evidence" in result:
            result["evidence"] = normalize_evidence(
                result["evidence"], result.get("raw_value")
            )
        return result

    normalized_record = normalize(record)
    ordered_segments = list(segments.items())

    medications = normalized_record.get("medications")
    if (
        isinstance(medications, dict)
        and isinstance(medications.get("items"), list)
        and not medications["items"]
    ):
        for position, (_, segment) in enumerate(ordered_segments[:-1]):
            question = segment["text"]
            if "?" not in question or not any(
                term in question for term in ("약", "복용", "먹었", "투약")
            ):
                continue
            answer_position = position + 1
            while (
                answer_position < len(ordered_segments)
                and "?" in ordered_segments[answer_position][1]["text"]
            ):
                answer_position += 1
            if answer_position >= len(ordered_segments):
                continue
            _, answer = ordered_segments[answer_position]
            evidence = {
                "text": answer["text"],
                "start": answer["start"],
                "end": answer["end"],
            }
            value = {
                "raw_value": answer["text"],
                "status": "needs_confirmation",
                "evidence": evidence,
            }
            if any(
                term in question
                for term in ("먹었", "복용하셨", "복용했", "투약했")
            ):
                if medications["last_dose"].get("status") == "not_mentioned":
                    medications["last_dose"] = value
            else:
                medications["items"].append(value)
            break

    questions: list[dict[str, Any]] = []
    answered_question_ids: set[Any] = set()
    position = 0
    while position < len(ordered_segments):
        _, segment = ordered_segments[position]
        if "?" not in segment["text"]:
            position += 1
            continue
        group_end = position + 1
        while (
            group_end < len(ordered_segments)
            and "?" in ordered_segments[group_end][1]["text"]
        ):
            group_end += 1
        if group_end < len(ordered_segments):
            answered_question_ids.update(
                item[0] for item in ordered_segments[position:group_end]
            )
        position = group_end
    for item in model_response.get("unresolved_questions", []):
        if not isinstance(item, dict):
            warnings.append("invalid unresolved question was discarded")
            continue
        segment_id = _resolve_segment_id(item.get("source_segment_id"), segments)
        if segment_id is None:
            warnings.append(
                "unresolved question references missing segment and was discarded"
            )
            continue
        if segment_id in answered_question_ids:
            warnings.append("answered question was removed from unresolved_questions")
            continue
        source = segments[segment_id]
        questions.append(
            {
                "text": source["text"],
                "start": source["start"],
                "end": source["end"],
                "topic": item.get("topic", "unknown"),
                "status": "unanswered",
            }
        )
    return {
        "schema_version": "clinical-record-v2",
        "clinical_record": normalized_record,
        "unresolved_questions": questions,
        "validation_warnings": warnings,
        "metadata": {
            "model": model_name,
            "prompt_version": prompt_version,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    }

