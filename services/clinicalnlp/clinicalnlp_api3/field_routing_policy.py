from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


CANONICAL_FIELD_ORDER = (
    "chief_complaint",
    "pain_assessment",
    "history_of_present_illness",
    "past_history",
    "medications",
    "drug_allergy",
    "social_history",
    "review_of_systems",
    "physical_examination",
    "impression",
    "treatment_plan",
)

CANONICAL_TO_DRAFT_FIELD = {
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
}
DRAFT_TO_CANONICAL_FIELD = {
    draft: canonical for canonical, draft in CANONICAL_TO_DRAFT_FIELD.items()
}

SYMPTOM = "symptom_or_sign"
DISEASE = "disease_or_diagnosis"
DRUG = "drug"
ALLERGY = "allergy"
ANATOMY = "anatomy"
PROCEDURE = "test_procedure_or_surgery"
VITAL = "vital_or_numeric"
DEVICE = "device"
_TERM_TYPE_ORDER = (
    SYMPTOM,
    DISEASE,
    DRUG,
    ALLERGY,
    ANATOMY,
    PROCEDURE,
    VITAL,
    DEVICE,
)
_TERM_TYPE_COLLECTION = {
    SYMPTOM: "emergency_terms",
    DISEASE: "emergency_terms",
    DRUG: "drug_terms",
    ALLERGY: "drug_terms",
    ANATOMY: "anatomy_terms",
    PROCEDURE: "procedure_terms",
    VITAL: "emergency_terms",
    DEVICE: "procedure_terms",
}


@dataclass(frozen=True, slots=True)
class FieldPolicy:
    definition: str
    allowed_term_types: frozenset[str]


FIELD_POLICIES = {
    "chief_complaint": FieldPolicy(
        "The explicit primary reason for the emergency visit, in patient wording.",
        frozenset({SYMPTOM, DISEASE, ANATOMY}),
    ),
    "pain_assessment": FieldPolicy(
        "Only explicitly stated pain assessment content; NRS requires an explicit score.",
        frozenset({SYMPTOM, ANATOMY, VITAL}),
    ),
    "history_of_present_illness": FieldPolicy(
        "Onset, course, change, and associated symptoms of the current problem.",
        frozenset({SYMPTOM, DISEASE, ANATOMY}),
    ),
    "past_history": FieldPolicy(
        "Previously diagnosed conditions and completed historical operations or "
        "procedures.",
        frozenset({DISEASE, PROCEDURE}),
    ),
    "medications": FieldPolicy(
        "Medication currently taken or explicitly reported as administered.",
        frozenset({DRUG}),
    ),
    "drug_allergy": FieldPolicy(
        "Explicit allergen or drug allergy and its reported reaction.",
        frozenset({ALLERGY, DRUG, SYMPTOM}),
    ),
    "social_history": FieldPolicy(
        "Explicit smoking and alcohol history; terminology candidates are not generated here.",
        frozenset(),
    ),
    "review_of_systems": FieldPolicy(
        "Patient-reported positive, negative, or uncertain answers to symptom review.",
        frozenset({SYMPTOM, ANATOMY}),
    ),
    "physical_examination": FieldPolicy(
        "Clinician-observed or measured examination findings, never inferred from symptoms.",
        frozenset({SYMPTOM, ANATOMY, VITAL, DEVICE}),
    ),
    "impression": FieldPolicy(
        "Explicitly stated diagnosis, suspected diagnosis, or differential with "
        "certainty preserved.",
        frozenset({DISEASE, SYMPTOM}),
    ),
    "treatment_plan": FieldPolicy(
        "Explicitly stated tests, medications, procedures, devices, or orders.",
        frozenset({PROCEDURE, DRUG, DEVICE}),
    ),
}

_FIELD_RANK = {field_id: index for index, field_id in enumerate(CANONICAL_FIELD_ORDER)}

_TERM_TYPE_FIELD_PRIORITY = {
    SYMPTOM: (
        "chief_complaint",
        "history_of_present_illness",
        "review_of_systems",
        "physical_examination",
        "impression",
        "drug_allergy",
    ),
    DISEASE: (
        "impression",
        "past_history",
        "history_of_present_illness",
        "chief_complaint",
    ),
    DRUG: ("medications", "drug_allergy", "treatment_plan"),
    ALLERGY: ("drug_allergy",),
    ANATOMY: (
        "physical_examination",
        "history_of_present_illness",
        "review_of_systems",
        "pain_assessment",
        "chief_complaint",
    ),
    PROCEDURE: ("treatment_plan", "past_history"),
    VITAL: ("physical_examination", "pain_assessment"),
    DEVICE: ("treatment_plan", "physical_examination"),
}

_SYMPTOM_UMLS_TYPES = frozenset({"T033", "T034", "T184", "T201"})
_DISEASE_UMLS_TYPES = frozenset(
    {"T019", "T020", "T037", "T046", "T047", "T048", "T049", "T190", "T191"}
)
_ANATOMY_UMLS_TYPES = frozenset(
    {"T017", "T018", "T021", "T022", "T023", "T024", "T029", "T030", "T031"}
)
_PROCEDURE_UMLS_TYPES = frozenset({"T058", "T059", "T060", "T061", "T063", "T203"})
_DEVICE_UMLS_TYPES = frozenset({"T074"})
_DRUG_UMLS_TYPES = frozenset(
    {
        "T103",
        "T109",
        "T116",
        "T121",
        "T123",
        "T125",
        "T126",
        "T127",
        "T129",
        "T130",
        "T131",
        "T195",
        "T200",
    }
)


@dataclass(frozen=True, slots=True)
class FieldEvidence:
    field_id: str
    raw_value: str


def canonical_field_id(field_id: str) -> str | None:
    if field_id in FIELD_POLICIES:
        return field_id
    return DRAFT_TO_CANONICAL_FIELD.get(field_id)


def _atomic_values(value: Any) -> Iterable[dict[str, Any]]:
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


def evidence_fields_by_segment(
    clinical_record: dict[str, Any],
    *,
    segments: Iterable[dict[str, Any]] = (),
) -> dict[str, tuple[FieldEvidence, ...]]:
    segment_ids_by_time: dict[tuple[object, object], str] = {}
    ambiguous_times: set[tuple[object, object]] = set()
    for segment in segments:
        if not isinstance(segment, dict) or segment.get("id") is None:
            continue
        key = (segment.get("start"), segment.get("end"))
        if key in segment_ids_by_time:
            ambiguous_times.add(key)
        else:
            segment_ids_by_time[key] = str(segment["id"])
    for key in ambiguous_times:
        segment_ids_by_time.pop(key, None)

    routed: dict[str, list[FieldEvidence]] = {}
    for field_id in CANONICAL_FIELD_ORDER:
        for value in _atomic_values(clinical_record.get(field_id)):
            evidence = value.get("evidence")
            segment_id = (
                evidence.get("source_segment_id")
                if isinstance(evidence, dict)
                else None
            )
            if segment_id is None and isinstance(evidence, dict):
                segment_id = segment_ids_by_time.get(
                    (evidence.get("start"), evidence.get("end"))
                )
            raw_value = str(value.get("raw_value") or "").strip()
            if segment_id is None or not str(segment_id) or not raw_value:
                continue
            normalized_segment_id = str(segment_id)
            entry = FieldEvidence(field_id=field_id, raw_value=raw_value)
            if entry not in routed.setdefault(normalized_segment_id, []):
                routed[normalized_segment_id].append(entry)
    return {key: tuple(value) for key, value in routed.items()}


def field_collection_hints_by_segment(
    clinical_record: dict[str, Any],
    *,
    segments: Iterable[dict[str, Any]] = (),
) -> dict[str, frozenset[str]]:
    """Map grounded record evidence to the vector collections it can use.

    Empty-policy fields such as social history intentionally contribute no
    hint. A missing hint therefore preserves the existing unrestricted safety
    path instead of suppressing terminology candidates.
    """

    routed: dict[str, set[str]] = {}
    for segment_id, evidence in evidence_fields_by_segment(
        clinical_record,
        segments=segments,
    ).items():
        collections = {
            _TERM_TYPE_COLLECTION[term_type]
            for item in evidence
            for term_type in FIELD_POLICIES[item.field_id].allowed_term_types
            if term_type in _TERM_TYPE_COLLECTION
        }
        if collections:
            routed[segment_id] = collections
    return {
        segment_id: frozenset(collections)
        for segment_id, collections in routed.items()
    }


def _semantic_types(candidate: dict[str, Any]) -> set[str]:
    provenance = candidate.get("provenance")
    values = (
        provenance.get("semantic_types")
        if isinstance(provenance, dict)
        else None
    )
    if not isinstance(values, (list, tuple)):
        return set()
    return {value for value in values if isinstance(value, str)}


def candidate_term_types(
    candidate: dict[str, Any],
    *,
    annotation_term_type: object = None,
) -> frozenset[str]:
    """Return the strongest available semantic categories for one candidate."""

    semantic_types = _semantic_types(candidate)
    if semantic_types:
        categories: set[str] = set()
        if semantic_types & _SYMPTOM_UMLS_TYPES:
            categories.add(SYMPTOM)
        if semantic_types & _DISEASE_UMLS_TYPES:
            categories.add(DISEASE)
        if semantic_types & _ANATOMY_UMLS_TYPES:
            categories.add(ANATOMY)
        if semantic_types & _PROCEDURE_UMLS_TYPES:
            categories.add(PROCEDURE)
        if semantic_types & _DEVICE_UMLS_TYPES:
            categories.add(DEVICE)
        if semantic_types & _DRUG_UMLS_TYPES:
            categories.add(DRUG)
        if categories:
            return frozenset(categories)

    entity_type = str(candidate.get("entity_type") or "").casefold()
    entity_type_map = {
        "symptom": SYMPTOM,
        "sign": SYMPTOM,
        "finding": SYMPTOM,
        "disease": DISEASE,
        "diagnosis": DISEASE,
        "ingredient": DRUG,
        "product": DRUG,
        "drug": DRUG,
        "procedure": PROCEDURE,
        "surgery": PROCEDURE,
        "anatomy": ANATOMY,
        "device": DEVICE,
    }
    if entity_type in entity_type_map:
        return frozenset({entity_type_map[entity_type]})

    collection = str(candidate.get("collection") or "").casefold()
    collection_types = {
        "drug_terms": frozenset({DRUG}),
        "procedure_terms": frozenset({PROCEDURE, DEVICE}),
        "anatomy_terms": frozenset({ANATOMY}),
        "emergency_terms": frozenset({SYMPTOM, DISEASE}),
    }
    if collection in collection_types:
        return collection_types[collection]

    if isinstance(annotation_term_type, str) and annotation_term_type in {
        SYMPTOM,
        DISEASE,
        DRUG,
        ALLERGY,
        ANATOMY,
        PROCEDURE,
        VITAL,
        DEVICE,
    }:
        return frozenset({annotation_term_type})
    return frozenset()


def candidate_allowed_for_field(
    field_id: str,
    candidate: dict[str, Any],
    *,
    annotation_term_type: object = None,
) -> bool:
    canonical = canonical_field_id(field_id)
    policy = FIELD_POLICIES.get(canonical or "")
    if policy is None or not policy.allowed_term_types:
        return False
    categories = candidate_term_types(
        candidate,
        annotation_term_type=annotation_term_type,
    )
    return bool(categories & policy.allowed_term_types)


def filter_candidates_for_field(
    field_id: str,
    candidates: Iterable[dict[str, Any]],
    *,
    annotation_term_type: object = None,
) -> list[dict[str, Any]]:
    return [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict)
        and candidate_allowed_for_field(
            field_id,
            candidate,
            annotation_term_type=annotation_term_type,
        )
    ]


def _preferred_fields(term_types: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    selected = set(term_types)
    for term_type in _TERM_TYPE_ORDER:
        if term_type not in selected:
            continue
        for field_id in _TERM_TYPE_FIELD_PRIORITY.get(term_type, ()):
            if field_id not in result:
                result.append(field_id)
    return tuple(result)


def choose_evidence_field(
    evidence: Iterable[FieldEvidence],
    *,
    source_text: str,
    candidates: Iterable[dict[str, Any]],
    annotation_term_type: object = None,
) -> str | None:
    """Choose a field already grounded to the segment, preferring the exact atom."""

    entries = list(evidence)
    candidate_list = [item for item in candidates if isinstance(item, dict)]
    categories: set[str] = set()
    for candidate in candidate_list:
        categories.update(
            candidate_term_types(
                candidate,
                annotation_term_type=annotation_term_type,
            )
        )
    declared_categories = {
        annotation_term_type
        if isinstance(annotation_term_type, str)
        and annotation_term_type in _TERM_TYPE_ORDER
        else ""
    } - {""}
    if not categories:
        categories.update(declared_categories)
    preferred = _preferred_fields((*declared_categories, *categories))
    preferred_rank = {field_id: index for index, field_id in enumerate(preferred)}

    normalized_source = source_text.strip().casefold()
    exact_entries = [
        entry
        for entry in entries
        if normalized_source
        and (
            normalized_source in entry.raw_value.casefold()
            or entry.raw_value.casefold() in normalized_source
        )
    ]
    # A translated candidate deliberately keeps the whole RAW sentence as its
    # source span because translated offsets cannot be presented as RAW
    # offsets. If that sentence grounds one or more draft atoms, candidates
    # must stay inside those atoms instead of being moved to an unrelated field
    # merely because its dictionary collection happens to be compatible.
    routed_entries = exact_entries or entries

    compatible: list[tuple[int, int, int, int, int, str]] = []
    for entry in routed_entries:
        policy = FIELD_POLICIES.get(entry.field_id)
        if policy is None or not categories & policy.allowed_term_types:
            continue
        normalized_value = entry.raw_value.casefold()
        exact_atom = entry in exact_entries
        compatible.append(
            (
                0 if exact_atom else 1,
                0 if declared_categories & policy.allowed_term_types else 1,
                -sum(
                    candidate_allowed_for_field(
                        entry.field_id,
                        candidate,
                        annotation_term_type=annotation_term_type,
                    )
                    for candidate in candidate_list
                ),
                preferred_rank.get(entry.field_id, len(preferred)),
                _FIELD_RANK.get(entry.field_id, len(_FIELD_RANK)),
                entry.field_id,
            )
        )
    if compatible:
        return min(compatible)[5]
    return None


def fallback_field_for_term_type(term_type: object) -> str | None:
    if not isinstance(term_type, str):
        return None
    fields = _TERM_TYPE_FIELD_PRIORITY.get(term_type, ())
    return fields[0] if fields else None
