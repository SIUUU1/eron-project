from __future__ import annotations

import re
from typing import Any


UMLS_LINK_THRESHOLD = 0.8
TOP_CANDIDATE_MARGIN = 0.10
MAX_APPLIED_CANDIDATES_PER_VALUE = 3

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
)

_DIRECT_MATCH_TYPES = frozenset(
    {
        "alias_exact",
        "official_exact",
        "stt_alias_exact",
        "approved_alias_candidate",
    }
)
_UMLS_MATCH_TYPE = "umls_dictionary_search"
_UNSUPPORTED_COLLECTIONS = frozenset({"kcd9_terms"})
_UNCERTAINTY_MARKERS = (
    "가능성",
    "의심",
    "추정",
    "불확실",
    "확인 필요",
    "것 같",
    "듯",
    "아마",
    "r/o",
)
_NEGATION_MARKERS = ("없", "않", "아니", "부인", "못")


def _atomic_values(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _atomic_values(item)
        return
    if not isinstance(value, dict):
        return
    if value.get("status") in {"confirmed", "needs_confirmation"}:
        yield value
        return
    for item in value.values():
        yield from _atomic_values(item)


def _display_value(candidate: dict[str, Any]) -> str:
    return str(
        candidate.get("canonical_en")
        or candidate.get("code_display")
        or candidate.get("canonical_ko")
        or ""
    ).strip()


def _candidate_id(
    segment_id: str,
    annotation_index: int,
    candidate: dict[str, Any],
) -> str:
    return "::".join(
        (
            segment_id,
            str(annotation_index),
            str(candidate.get("collection") or "unknown"),
            str(candidate.get("entity_id") or "unknown"),
        )
    )


def _candidate_descriptor(
    segment_id: str,
    annotation_index: int,
    candidate: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any] | None:
    entity_id = candidate.get("entity_id")
    collection = candidate.get("collection")
    display_value = _display_value(candidate)
    if (
        not isinstance(entity_id, str)
        or not entity_id
        or not isinstance(collection, str)
        or not collection
        or collection in _UNSUPPORTED_COLLECTIONS
        or not display_value
    ):
        return None
    return {
        "candidate_id": _candidate_id(segment_id, annotation_index, candidate),
        "collection": collection,
        "entity_id": entity_id,
        "display_value": display_value,
        "source": source,
        "retrieval_score": float(candidate.get("retrieval_score") or 0.0),
    }


def _ranked_unique_candidates(
    segment_id: str,
    annotation_index: int,
    candidates: list[dict[str, Any]],
    *,
    source: str,
) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        descriptor = _candidate_descriptor(
            segment_id,
            annotation_index,
            candidate,
            source=source,
        )
        if descriptor is None:
            continue
        key = (descriptor["collection"], descriptor["entity_id"])
        current = unique.get(key)
        if current is None or descriptor["retrieval_score"] > current["retrieval_score"]:
            unique[key] = descriptor
    return sorted(
        unique.values(),
        key=lambda item: (
            -item["retrieval_score"],
            item["collection"],
            item["entity_id"],
        ),
    )


def _clear_top_candidate(
    ranked: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not ranked:
        return None
    if len(ranked) == 1:
        return ranked[0]
    if ranked[0]["retrieval_score"] - ranked[1]["retrieval_score"] >= TOP_CANDIDATE_MARGIN:
        return ranked[0]
    return None


def _umls_link_is_eligible(candidate: dict[str, Any]) -> bool:
    provenance = candidate.get("provenance")
    similarity = provenance.get("similarity") if isinstance(provenance, dict) else None
    return (
        isinstance(similarity, (int, float))
        and not isinstance(similarity, bool)
        and float(similarity) >= UMLS_LINK_THRESHOLD
    )


def build_draft_normalization_plan(
    clinical_record: dict[str, Any],
    segments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build deterministic exact replacements and a bounded UMLS model payload."""

    segment_by_id = {
        str(segment.get("id")): segment
        for segment in segments
        if isinstance(segment, dict) and segment.get("id") is not None
    }
    segment_list = list(segment_by_id.values())
    direct_suggestions: list[dict[str, Any]] = []
    model_fields: list[dict[str, Any]] = []

    for field_id in CANONICAL_FIELD_IDS:
        for atom_index, atom in enumerate(_atomic_values(clinical_record.get(field_id))):
            original_value = str(atom.get("raw_value") or "").strip()
            evidence = atom.get("evidence")
            segment_id = ""
            if isinstance(evidence, dict):
                if evidence.get("source_segment_id") is not None:
                    segment_id = str(evidence.get("source_segment_id"))
                else:
                    matches = [
                        segment
                        for segment in segment_list
                        if (
                            evidence.get("start") is not None
                            and evidence.get("end") is not None
                            and segment.get("start") == evidence.get("start")
                            and segment.get("end") == evidence.get("end")
                        )
                        or (
                            isinstance(evidence.get("text"), str)
                            and evidence.get("text")
                            in {
                                segment.get("text"),
                                segment.get("raw_text"),
                                segment.get("corrected_text"),
                            }
                        )
                    ]
                    if len(matches) == 1:
                        segment_id = str(matches[0].get("id"))
            segment = segment_by_id.get(segment_id)
            if not original_value or segment is None:
                continue
            atom_id = f"{field_id}:{atom_index}:{segment_id}"
            direct_value = original_value
            applied_direct: list[dict[str, Any]] = []
            umls_allowed: list[dict[str, Any]] = []
            seen_umls_ids: set[str] = set()

            for annotation_index, annotation in enumerate(segment.get("annotations", [])):
                if not isinstance(annotation, dict):
                    continue
                source_span = annotation.get("source_span")
                source_text = (
                    str(source_span.get("text") or "")
                    if isinstance(source_span, dict)
                    else ""
                )
                candidates = [
                    candidate
                    for candidate in annotation.get("candidates", [])
                    if isinstance(candidate, dict)
                ]
                exact = [
                    candidate
                    for candidate in candidates
                    if str(candidate.get("match_type") or "").casefold()
                    in _DIRECT_MATCH_TYPES
                ]
                if source_text and source_text in direct_value and exact:
                    top = _clear_top_candidate(
                        _ranked_unique_candidates(
                            segment_id,
                            annotation_index,
                            exact,
                            source="RAW_EXACT",
                        )
                    )
                    if top is not None and len(applied_direct) < MAX_APPLIED_CANDIDATES_PER_VALUE:
                        direct_value = direct_value.replace(
                            source_text,
                            top["display_value"],
                            1,
                        )
                        applied_direct.append(top)
                    continue

                umls = [
                    candidate
                    for candidate in candidates
                    if str(candidate.get("match_type") or "").casefold()
                    == _UMLS_MATCH_TYPE
                    and _umls_link_is_eligible(candidate)
                ]
                top = _clear_top_candidate(
                    _ranked_unique_candidates(
                        segment_id,
                        annotation_index,
                        umls,
                        source="UMLS",
                    )
                )
                if top is not None and top["candidate_id"] not in seen_umls_ids:
                    seen_umls_ids.add(top["candidate_id"])
                    umls_allowed.append(top)

            if direct_value != original_value:
                direct_suggestions.append(
                    {
                        "field_id": field_id,
                        "atom_id": atom_id,
                        "original_value": original_value,
                        "suggested_value": direct_value,
                        "applied_candidates": applied_direct,
                    }
                )
                continue
            if umls_allowed:
                model_fields.append(
                    {
                        "field_id": field_id,
                        "atom_id": atom_id,
                        "original_value": original_value,
                        "status": atom.get("status"),
                        "source_segment_id": segment_id,
                        "allowed_candidates": umls_allowed[
                            :MAX_APPLIED_CANDIDATES_PER_VALUE
                        ],
                    }
                )

    return direct_suggestions, {"fields": model_fields}


def _normalized_remainder(value: str, display_values: list[str]) -> str:
    remainder = value.casefold()
    for display_value in sorted(display_values, key=len, reverse=True):
        remainder = remainder.replace(display_value.casefold(), "")
    return "".join(re.findall(r"[0-9a-z가-힣]+", remainder))


def _is_subsequence(value: str, source: str) -> bool:
    position = 0
    for character in value:
        position = source.find(character, position)
        if position < 0:
            return False
        position += 1
    return True


def _preserves_assertion(original: str, suggested: str) -> bool:
    original_folded = original.casefold()
    suggested_folded = suggested.casefold()
    if any(marker in original_folded for marker in _UNCERTAINTY_MARKERS):
        if not any(marker in suggested_folded for marker in _UNCERTAINTY_MARKERS):
            return False
    if any(marker in original_folded for marker in _NEGATION_MARKERS):
        if not any(marker in suggested_folded for marker in _NEGATION_MARKERS):
            return False
    if "?" in original and "?" not in suggested:
        return False
    return True


def ground_model_draft_suggestions(
    model_output: dict[str, Any],
    model_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Release only suggestions composed from exact supplied IDs and source text."""

    allowed_by_atom = {
        field["atom_id"]: field
        for field in model_payload.get("fields", [])
        if isinstance(field, dict) and isinstance(field.get("atom_id"), str)
    }
    grounded: list[dict[str, Any]] = []
    seen_atoms: set[str] = set()
    for suggestion in model_output.get("draft_suggestions", []):
        if not isinstance(suggestion, dict):
            continue
        atom_id = suggestion.get("atom_id")
        field = allowed_by_atom.get(atom_id)
        if field is None or atom_id in seen_atoms:
            continue
        if suggestion.get("field_id") != field.get("field_id"):
            continue
        allowed = {
            candidate["candidate_id"]: candidate
            for candidate in field.get("allowed_candidates", [])
            if isinstance(candidate, dict)
        }
        applied_ids = suggestion.get("applied_candidate_ids")
        if not isinstance(applied_ids, list) or not applied_ids:
            continue
        if len(applied_ids) > MAX_APPLIED_CANDIDATES_PER_VALUE:
            continue
        if len(set(applied_ids)) != len(applied_ids) or any(
            candidate_id not in allowed for candidate_id in applied_ids
        ):
            continue
        suggested_value = suggestion.get("suggested_value")
        original_value = str(field.get("original_value") or "")
        if (
            not isinstance(suggested_value, str)
            or not suggested_value.strip()
            or suggested_value.strip() == original_value
            or len(suggested_value) > len(original_value) + 256
        ):
            continue
        suggested_value = suggested_value.strip()
        candidates = [allowed[candidate_id] for candidate_id in applied_ids]
        display_values = [candidate["display_value"] for candidate in candidates]
        if any(display_value not in suggested_value for display_value in display_values):
            continue
        if not _preserves_assertion(original_value, suggested_value):
            continue
        original_compact = "".join(re.findall(r"[0-9a-z가-힣]+", original_value.casefold()))
        remainder = _normalized_remainder(suggested_value, display_values)
        if not _is_subsequence(remainder, original_compact):
            continue
        grounded.append(
            {
                "field_id": field["field_id"],
                "atom_id": atom_id,
                "original_value": original_value,
                "suggested_value": suggested_value,
                "applied_candidates": candidates,
            }
        )
        seen_atoms.add(atom_id)
    return grounded

