from __future__ import annotations

import copy
import re
from typing import Any

from .runtime_validation import validate_clinical_workflow


SCHEMA_VERSION = "clinical-workflow-v2"

PROCESSING_STATUS_VALUES = frozenset({"completed", "partial", "failed"})
RECORD_STATUS_VALUES = frozenset({"NOT_STARTED", "DRAFT", "COMPLETED"})
WORKFLOW_PHASE_VALUES = frozenset(
    {
        "STT_INPUT",
        "DRAFT_GENERATION",
        "CLINICIAN_REVIEW",
        "FINALIZATION",
        "POST_SIGN_EDIT",
    }
)
VALIDATION_STATUS_VALUES = frozenset({"PASS", "REVIEW_REQUIRED", "BLOCK"})
INFORMATION_STATUS_VALUES = frozenset(
    {"PRESENT", "NONE", "NOT_ASSESSED", "UNCERTAIN"}
)
SUGGESTION_STATUS_VALUES = frozenset(
    {"UNCHANGED", "AUTO_SUGGESTED", "UNRESOLVED"}
)

LEGACY_TO_CANONICAL_FIELD_ID = {
    "chief": "chief_complaint",
    "pain": "pain_assessment",
    "history": "history_of_present_illness",
    "past-history": "past_history",
    "medication": "medications",
    "allergy": "drug_allergy",
    "social": "social_history",
    "review-of-systems": "review_of_systems",
    "physical": "physical_examination",
    "treatment-plan": "treatment_plan",
    "impression": "impression",
}

CANONICAL_FIELD_IDS = (
    "chief_complaint",
    "pain_assessment",
    "history_of_present_illness",
    "past_history",
    "medications",
    "drug_allergy",
    "social_history",
    "review_of_systems",
    "physical_examination",
    "treatment_plan",
    "impression",
    "outcome",
)

GUARDRAIL_TO_CANONICAL_FIELD_ID = {
    "present_illness": "history_of_present_illness",
    "past_medical_history": "past_history",
    "surgical_history": "past_history",
    "medication_history": "medications",
    "allergy": "drug_allergy",
    "physical_exam": "physical_examination",
    "plan": "treatment_plan",
    "assessment_candidates": "impression",
}

_LEGACY_INFORMATION_STATUS = {
    "filled": "PRESENT",
    "needs_review": "UNCERTAIN",
    "unknown": "NOT_ASSESSED",
    "empty": "NOT_ASSESSED",
}

_AMBIGUOUS_MEDICAL_TERMS = ("리네일러",)


def canonical_field_id(field_id: str) -> str:
    return LEGACY_TO_CANONICAL_FIELD_ID.get(
        field_id,
        GUARDRAIL_TO_CANONICAL_FIELD_ID.get(field_id, field_id),
    )


def _atomic_values(value: Any):
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


def _is_explicit_negation(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(
        re.search(
            r"(?:없(?:습니다|어요|다|음|었)|아니(?:요|다|었습니다)|"
            r"하지\s*않|안\s+\S+|부인)",
            text,
        )
    )


def _is_uncertain(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(
        re.search(
            r"(?:모르|기억(?:이)?\s*(?:안|못)|확실하지|불확실|정확하지|"
            r"것\s*같|듯(?:해|합니|하|$)|아마|확인\s*필요)",
            text,
        )
    )


def _assertion_subject(value: Any) -> tuple[str, ...]:
    text = str(value or "").casefold()
    text = re.sub(
        r"(?:있(?:습니다|어요|다|음)|없(?:습니다|어요|다|음|었(?:습니다|어요|다)?)|"
        r"아니(?:요|다|었습니다)|하지\s*않(?:습니다|아요|다|음)?|"
        r"안\s*(?:합니다|해요|함)|부인(?:합니다|함)?|진단받(?:은|았습니다|았어요|음)?)",
        " ",
        text,
    )
    terms: list[str] = []
    for token in re.findall(r"[0-9a-z가-힣]+", text):
        token = re.sub(r"(?:은|는|이|가|을|를|도)$", "", token)
        if token and token not in {"특별히", "병력", "과거력", "현재"}:
            terms.append(token)
    return tuple(terms)


def _has_conflict(confirmed: list[dict[str, Any]]) -> bool:
    positive_subjects: set[tuple[str, ...]] = set()
    negative_subjects: set[tuple[str, ...]] = set()
    for atom in confirmed:
        subject = _assertion_subject(atom.get("raw_value"))
        if not subject:
            continue
        target = negative_subjects if _is_explicit_negation(atom.get("raw_value")) else positive_subjects
        target.add(subject)
    return bool(positive_subjects & negative_subjects)


def _information_status(clinical_source: Any, legacy_status: str) -> str:
    atoms = list(_atomic_values(clinical_source))
    if atoms and all(
        atom.get("status") in {"not_mentioned", "asked_but_unanswered"}
        for atom in atoms
    ):
        return "NOT_ASSESSED"
    if any(
        atom.get("status") == "needs_confirmation"
        or _is_uncertain(atom.get("raw_value"))
        or any(
            term in str(atom.get("raw_value") or "")
            for term in _AMBIGUOUS_MEDICAL_TERMS
        )
        for atom in atoms
    ):
        return "UNCERTAIN"
    confirmed = [atom for atom in atoms if atom.get("status") == "confirmed"]
    if confirmed:
        if _has_conflict(confirmed):
            return "UNCERTAIN"
        if all(_is_explicit_negation(atom.get("raw_value")) for atom in confirmed):
            return "NONE"
        return "PRESENT"
    return _LEGACY_INFORMATION_STATUS.get(legacy_status, "NOT_ASSESSED")


def _v2_field(
    field_id: str,
    source: Any,
    clinical_source: Any = None,
) -> dict[str, Any]:
    field = source if isinstance(source, dict) else {}
    legacy_status = str(field.get("status") or "empty")
    information_status = _information_status(clinical_source, legacy_status)
    value = str(field.get("value") or "")
    ai_original_value = str(field.get("ai_original_value") or value)
    suggestion_status = str(field.get("suggestion_status") or "UNCHANGED")
    if suggestion_status not in SUGGESTION_STATUS_VALUES:
        suggestion_status = "UNCHANGED"
    applied_candidates = [
        copy.deepcopy(candidate)
        for candidate in field.get("applied_candidates", [])
        if isinstance(candidate, dict)
    ]
    return {
        "field_id": field_id,
        "value": value,
        "ai_original_value": ai_original_value,
        "suggestion_status": suggestion_status,
        "applied_candidates": applied_candidates,
        "information_status": information_status,
        "evidence": copy.deepcopy(field.get("evidence") or []),
    }


def _unsupported_normalization_review_items(
    clinical_record: dict[str, Any],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for field_id in CANONICAL_FIELD_IDS:
        for atom_index, atom in enumerate(_atomic_values(clinical_record.get(field_id))):
            provenance = atom.get("normalization_provenance")
            if (
                not isinstance(provenance, dict)
                or provenance.get("basis_type") != "UNVERIFIED_MODEL_OUTPUT"
            ):
                continue
            evidence = atom.get("evidence") if isinstance(atom.get("evidence"), dict) else {}
            segment_id = evidence.get("source_segment_id") or evidence.get("segment_id")
            raw_value = str(atom.get("raw_value") or "")
            items.append(
                {
                    "id": f"normalization:{field_id}:{segment_id or atom_index}",
                    "type": "normalization_unsupported",
                    "field_id": field_id,
                    "segment_id": segment_id,
                    "source": raw_value,
                    "evidence": copy.deepcopy(evidence),
                    "proposed_value": str(provenance.get("proposed_value") or ""),
                    "needs_review": True,
                }
            )
    return items


def _unresolved_uncertainty_review_items(
    fields: dict[str, dict[str, Any]],
    clinical_record: dict[str, Any],
    existing_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    fields_already_exposed = {
        str(item.get("field_id"))
        for item in existing_items
        if isinstance(item, dict) and item.get("needs_review") is True
    }
    items: list[dict[str, Any]] = []
    for field_id, field in fields.items():
        if (
            field.get("information_status") != "UNCERTAIN"
            or field_id in fields_already_exposed
        ):
            continue
        atoms = [
            atom
            for atom in _atomic_values(clinical_record.get(field_id))
            if atom.get("raw_value") not in (None, "")
        ]
        source_values = list(
            dict.fromkeys(str(atom.get("raw_value") or "").strip() for atom in atoms)
        )
        source_values = [value for value in source_values if value]
        evidence_values = [
            copy.deepcopy(atom.get("evidence"))
            for atom in atoms
            if isinstance(atom.get("evidence"), dict)
        ]
        first_evidence = evidence_values[0] if evidence_values else {}
        segment_id = first_evidence.get("source_segment_id") or first_evidence.get(
            "segment_id"
        )
        source = "\n".join(source_values) or str(field.get("value") or "")
        items.append(
            {
                "id": f"uncertainty:{field_id}:{segment_id or 'field'}",
                "type": "unresolved_uncertainty",
                "field_id": field_id,
                "segment_id": segment_id,
                "source": source,
                "evidence": source,
                "evidence_details": evidence_values,
                "candidates": [],
                "needs_review": True,
            }
        )
    return items


def to_clinical_workflow_v2(
    v1_document: dict[str, Any],
    *,
    policy_evidence_provider: Any | None = None,
) -> dict[str, Any]:
    """Add v2 lifecycle, information states, and non-destructive field issues."""

    result = copy.deepcopy(v1_document)
    legacy_draft = result.get("draft") if isinstance(result.get("draft"), dict) else {}
    legacy_fields = (
        legacy_draft.get("fields")
        if isinstance(legacy_draft.get("fields"), dict)
        else {}
    )
    api2 = result.get("api2") if isinstance(result.get("api2"), dict) else {}
    clinical_record = (
        api2.get("clinical_record")
        if isinstance(api2.get("clinical_record"), dict)
        else {}
    )
    canonical_fields = {
        canonical_id: _v2_field(
            canonical_id,
            legacy_fields.get(legacy_id),
            clinical_record.get(canonical_id),
        )
        for legacy_id, canonical_id in LEGACY_TO_CANONICAL_FIELD_ID.items()
    }
    canonical_fields["outcome"] = _v2_field("outcome", None)

    review_items = copy.deepcopy(legacy_draft.get("review_items") or [])
    for item in review_items:
        if isinstance(item, dict) and isinstance(item.get("field_id"), str):
            item["field_id"] = canonical_field_id(item["field_id"])
    normalization_review_items = _unsupported_normalization_review_items(
        clinical_record
    )
    review_items.extend(normalization_review_items)
    for item in normalization_review_items:
        canonical_fields[item["field_id"]]["information_status"] = "UNCERTAIN"
    review_items.extend(
        _unresolved_uncertainty_review_items(
            canonical_fields,
            clinical_record,
            review_items,
        )
    )
    for item in review_items:
        if not isinstance(item, dict):
            continue
        field_id = item.get("field_id")
        field = canonical_fields.get(field_id)
        candidates = item.get("candidates")
        if (
            field is not None
            and item.get("needs_review") is True
            and isinstance(candidates, list)
            and not candidates
            and field["suggestion_status"] == "UNCHANGED"
        ):
            field["suggestion_status"] = "UNRESOLVED"

    result["schema_version"] = SCHEMA_VERSION
    audit = result.get("audit")
    if isinstance(audit, dict):
        versions = audit.get("versions")
        if isinstance(versions, dict):
            versions["workflow_schema"] = SCHEMA_VERSION
    result["record_status"] = "DRAFT"
    result["workflow_phase"] = "DRAFT_GENERATION"
    result["completed_at"] = None
    result["draft"] = {
        "fields": {
            field_id: canonical_fields[field_id]
            for field_id in CANONICAL_FIELD_IDS
        },
        "review_items": review_items,
    }
    result["validation"] = validate_clinical_workflow(
        result,
        policy_evidence_provider=policy_evidence_provider,
    )
    return result

