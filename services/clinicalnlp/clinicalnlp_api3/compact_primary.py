from __future__ import annotations

import copy
from typing import Any, Mapping

from .compact_record_v3 import CANONICAL_FIELD_IDS


CANONICAL_TO_LEGACY_FIELD_ID = {
    "chief_complaint": "chief",
    "pain_assessment": "pain",
    "history_of_present_illness": "history",
    "past_history": "past-history",
    "medications": "medication",
    "drug_allergy": "allergy",
    "social_history": "social",
    "review_of_systems": "review-of-systems",
    "physical_examination": "physical",
    "impression": "impression",
    "treatment_plan": "treatment-plan",
    "outcome": "outcome",
}


def _field_information_status(
    field: Mapping[str, Any],
    facts: Mapping[str, Any],
    field_validation_status: str,
) -> str:
    generation_status = field.get("generation_status")
    if generation_status is None:
        generation_status = (
            "GENERATED"
            if str(field.get("text") or "").strip() and field.get("fact_refs")
            else "NOT_MENTIONED"
        )
    if generation_status == "FAILED" or field_validation_status != "PASS":
        return "UNCERTAIN"
    if generation_status == "NOT_MENTIONED":
        return "NOT_ASSESSED"
    assertions = {
        str(facts[ref].get("assertion"))
        for ref in field.get("fact_refs", [])
        if ref in facts and isinstance(facts[ref], Mapping)
    }
    if not assertions or "UNCERTAIN" in assertions:
        return "UNCERTAIN"
    if assertions == {"DENIED"}:
        return "NONE"
    return "PRESENT"


def _fact_segment_ids(
    field: Mapping[str, Any], facts: Mapping[str, Any]
) -> list[str]:
    result: list[str] = []
    for fact_ref in field.get("fact_refs", []):
        fact = facts.get(fact_ref)
        if not isinstance(fact, Mapping):
            continue
        for segment_id in fact.get("segments", []):
            if isinstance(segment_id, str) and segment_id not in result:
                result.append(segment_id)
    return result


def _field_evidence(
    field: Mapping[str, Any],
    facts: Mapping[str, Any],
    segments_by_id: Mapping[str, Mapping[str, Any]],
    translations: Mapping[str, str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for segment_id in _fact_segment_ids(field, facts):
        segment = segments_by_id.get(segment_id)
        if not isinstance(segment, Mapping):
            continue
        result.append(
            {
                "text": segment.get("raw_text"),
                "start": segment.get("start"),
                "end": segment.get("end"),
                "segment_id": segment_id,
                "raw_text": segment.get("raw_text"),
                "corrected_text": segment.get("corrected_text"),
                **(
                    {"translated_text_en": translations[segment_id]}
                    if translations.get(segment_id)
                    else {}
                ),
            }
        )
    return result


def project_compact_primary_draft(
    compact_record: Mapping[str, Any],
    compact_validation: Mapping[str, Any],
    api3_document: Mapping[str, Any],
    translated_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Project model-authored Compact v3 text into the legacy UI envelope.

    The adapter never rewrites clinical prose. It only maps identifiers,
    source segments, and deterministic validation state.
    """

    facts = (
        compact_record.get("facts")
        if isinstance(compact_record.get("facts"), Mapping)
        else {}
    )
    compact_fields = (
        compact_record.get("fields")
        if isinstance(compact_record.get("fields"), Mapping)
        else {}
    )
    field_statuses = (
        compact_validation.get("field_statuses")
        if isinstance(compact_validation.get("field_statuses"), Mapping)
        else {}
    )
    default_field_status = (
        "PASS" if compact_validation.get("status") == "PASS" else "BLOCK"
    )
    segments_by_id = {
        str(segment.get("id")): segment
        for segment in api3_document.get("segments", [])
        if isinstance(segment, Mapping) and isinstance(segment.get("id"), str)
    }
    translations = {
        str(item.get("segment_id")): str(item.get("translated_text_en") or "").strip()
        for item in translated_segments or []
        if isinstance(item, Mapping)
        and isinstance(item.get("segment_id"), str)
        and str(item.get("translated_text_en") or "").strip()
    }

    projected_fields: dict[str, dict[str, Any]] = {}
    for canonical_id in CANONICAL_FIELD_IDS:
        legacy_id = CANONICAL_TO_LEGACY_FIELD_ID[canonical_id]
        field = compact_fields.get(canonical_id)
        field = field if isinstance(field, Mapping) else {}
        generation_status_value = field.get("generation_status")
        generation_status = (
            str(generation_status_value)
            if generation_status_value is not None
            else "GENERATED"
            if str(field.get("text") or "").strip() and field.get("fact_refs")
            else "NOT_MENTIONED"
        )
        field_validation_status = str(
            field_statuses.get(canonical_id) or default_field_status
        )
        text = (
            str(field.get("text") or "")
            if generation_status == "GENERATED"
            else ""
        )
        needs_review = (
            generation_status == "FAILED" or field_validation_status != "PASS"
        )
        projected_fields[legacy_id] = {
            "value": text,
            "ai_original_value": text,
            "status": (
                "needs_review"
                if needs_review
                else "filled"
                if generation_status == "GENERATED"
                else "empty"
            ),
            "suggestion_status": "UNRESOLVED" if needs_review else "UNCHANGED",
            "applied_candidates": [],
            "information_status": _field_information_status(
                field,
                facts,
                field_validation_status,
            ),
            "evidence": _field_evidence(
                field,
                facts,
                segments_by_id,
                translations,
            ),
        }

    review_items: list[dict[str, Any]] = []
    for index, issue in enumerate(compact_validation.get("issues", [])):
        if not isinstance(issue, Mapping):
            continue
        for canonical_id in issue.get("field_ids", []) or [None]:
            legacy_id = CANONICAL_TO_LEGACY_FIELD_ID.get(str(canonical_id or ""))
            review_items.append(
                {
                    "id": f"compact-v3:{index}:{canonical_id or 'workflow'}",
                    "type": "compact_v3_validation",
                    "field_id": legacy_id or "workflow",
                    "source": (
                        projected_fields.get(legacy_id, {}).get("value", "")
                        if legacy_id
                        else ""
                    ),
                    "evidence": "",
                    "candidates": [],
                    "validation_reasons": [
                        str(issue.get("issue_code") or issue.get("code") or "")
                    ],
                    "compact_issue": copy.deepcopy(dict(issue)),
                    "needs_review": True,
                }
            )
    return {"fields": projected_fields, "review_items": review_items}


def candidate_field_routes(
    compact_record: Mapping[str, Any],
    candidate_snapshots: Mapping[str, Mapping[str, Any]],
) -> dict[tuple[str, int], str]:
    """Map retrieved annotation positions to Compact-selected UI fields."""

    facts = compact_record.get("facts")
    fields = compact_record.get("fields")
    if not isinstance(facts, Mapping) or not isinstance(fields, Mapping):
        return {}
    routes: dict[tuple[str, int], str] = {}
    for canonical_id, field in fields.items():
        if not isinstance(field, Mapping):
            continue
        legacy_id = CANONICAL_TO_LEGACY_FIELD_ID.get(str(canonical_id))
        if legacy_id is None:
            continue
        for fact_ref in field.get("fact_refs", []):
            fact = facts.get(fact_ref)
            if not isinstance(fact, Mapping):
                continue
            candidate_ref = fact.get("candidate_ref")
            snapshot = candidate_snapshots.get(str(candidate_ref or ""))
            if not isinstance(snapshot, Mapping):
                continue
            query_id = str(snapshot.get("query_id") or "")
            segment_id = str(snapshot.get("segment_id") or "")
            marker = query_id.rsplit(":a", 1)
            if len(marker) != 2:
                continue
            annotation_text = marker[1].split(":", 1)[0]
            if annotation_text.isdigit() and segment_id:
                routes.setdefault((segment_id, int(annotation_text)), legacy_id)
    return routes


__all__ = [
    "CANONICAL_TO_LEGACY_FIELD_ID",
    "candidate_field_routes",
    "project_compact_primary_draft",
]
