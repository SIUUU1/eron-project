from __future__ import annotations

import copy
import json
from typing import Any, Iterable, Mapping

from .clinical_llm import InvalidClinicalLlmOutput, _validate_schema
from .compact_record_v3 import (
    CANONICAL_FIELD_IDS,
    validate_compact_record,
)


SCHEMA_VERSION = "clinical-record-compact-v3.1"
FACT_CHUNK_SCHEMA_VERSION = "clinical-record-compact-facts-v1"
FIELD_SCHEMA_VERSION = "clinical-record-compact-fields-v1"
PROMPT_VERSION = "clinical-record-compact-v3.1-lean-v1"

MAX_FACTS = 96
MAX_SEGMENTS_PER_FACT = 8
MAX_FACT_REFS_PER_FIELD = 64
MAX_FIELD_TEXT_LENGTH = 2048
MAX_FACT_TEXT_LENGTH = 512
MAX_MEASUREMENT_PROPERTIES = 8
MAX_CANDIDATES_PER_CHUNK = 64
MAX_SEGMENTS_PER_CHUNK = 16
MAX_CHUNKS = 8
# Eight initial Fact chunks + one all-fields call + one bounded repair and
# regeneration + three fixed field-group fallbacks.
MAX_LOGICAL_LLM_CALLS = 14
MAX_SPLIT_DEPTH = 4

ASSERTIONS = ("DENIED", "PRESENT", "UNCERTAIN")
FACT_TYPES = ("MATCHED_TERM", "MEASUREMENT", "NARRATIVE", "UNMATCHED_TERM")
CLINICAL_ACTS = ("ASSESSMENT", "EXAM", "OUTCOME", "PLAN", "UNKNOWN")
FIELD_GROUPS = (
    (
        "chief_complaint",
        "pain_assessment",
        "history_of_present_illness",
        "past_history",
        "medications",
        "drug_allergy",
        "social_history",
        "review_of_systems",
    ),
    ("physical_examination", "impression"),
    ("treatment_plan", "outcome"),
)

_STATUS_RANK = {"PASS": 0, "REVIEW_REQUIRED": 1, "BLOCK": 2}


def minimal_candidate_projection(
    snapshots: Mapping[str, Mapping[str, Any]],
    *,
    segment_ids: Iterable[str] | None = None,
    limit: int = MAX_CANDIDATES_PER_CHUNK,
) -> list[dict[str, Any]]:
    """Return only the terminology attributes Gemma needs for selection."""

    restrict_segments = segment_ids is not None
    allowed = set(segment_ids or ())
    projected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate_ref, snapshot in snapshots.items():
        if not isinstance(snapshot, Mapping):
            continue
        segment_id = snapshot.get("segment_id")
        if not isinstance(segment_id, str) or (
            restrict_segments and segment_id not in allowed
        ):
            continue
        surface = str(snapshot.get("source_span") or "").strip()
        canonical = str(snapshot.get("canonical") or "").strip()
        if not surface or not canonical:
            continue
        key = (segment_id, surface.casefold(), canonical.casefold())
        if key in seen:
            continue
        seen.add(key)
        semantic_types = snapshot.get("semantic_types")
        projected.append(
            {
                "candidate_ref": str(candidate_ref),
                "segment_id": segment_id,
                "surface": surface,
                "canonical": canonical,
                "semantic_types": [
                    value
                    for value in semantic_types or []
                    if isinstance(value, str) and value.strip()
                ],
                "source": str(snapshot.get("retrieval_source") or ""),
            }
        )
        if len(projected) >= limit:
            break
    return projected


def _segments_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_SEGMENTS_PER_FACT,
        "uniqueItems": True,
        "items": {"type": "string", "minLength": 1},
    }


def _fact_variant(
    fact_type: str,
    required: list[str],
    extra: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "assertion", "segments", *required],
        "properties": {
            "type": {"const": fact_type},
            "assertion": {"type": "string", "enum": list(ASSERTIONS)},
            "segments": _segments_schema(),
            "fact_type": {"type": "string", "enum": list(CLINICAL_ACTS)},
            "supersedes_fact_id": {"type": "string", "minLength": 1},
            **copy.deepcopy(dict(extra)),
        },
    }


def _fact_schema() -> dict[str, Any]:
    scalar = {"type": ["number", "string", "null"]}
    return {
        "oneOf": [
            _fact_variant(
                "MATCHED_TERM",
                ["candidate_ref"],
                {"candidate_ref": {"type": "string", "minLength": 1}},
            ),
            _fact_variant(
                "UNMATCHED_TERM",
                ["text"],
                {
                    "text": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_FACT_TEXT_LENGTH,
                    }
                },
            ),
            _fact_variant(
                "NARRATIVE",
                ["text"],
                {
                    "text": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_FACT_TEXT_LENGTH,
                    }
                },
            ),
            _fact_variant(
                "MEASUREMENT",
                ["values"],
                {
                    "values": {
                        "type": "object",
                        "minProperties": 2,
                        "maxProperties": MAX_MEASUREMENT_PROPERTIES,
                        "required": ["kind", "value"],
                        "properties": {
                            "kind": {"type": "string", "minLength": 1},
                            "value": scalar,
                        },
                        "additionalProperties": scalar,
                    }
                },
            ),
        ]
    }


def _facts_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "maxProperties": MAX_FACTS,
        "additionalProperties": _fact_schema(),
    }


def _field_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["text", "fact_refs"],
        "properties": {
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_FIELD_TEXT_LENGTH,
            },
            "fact_refs": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_FACT_REFS_PER_FIELD,
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
        },
    }


def _format(name: str, schema: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": copy.deepcopy(dict(schema)),
        },
    }


def lean_record_response_format() -> dict[str, Any]:
    return _format(
        "clinical_record_compact_v3_1",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "facts", "fields"],
            "properties": {
                "schema_version": {"const": SCHEMA_VERSION},
                "facts": _facts_schema(),
                "fields": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        field_id: _field_schema()
                        for field_id in CANONICAL_FIELD_IDS
                    },
                },
            },
        },
    )


def fact_chunk_response_format() -> dict[str, Any]:
    return _format(
        "clinical_record_compact_facts_v1",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "facts"],
            "properties": {
                "schema_version": {"const": FACT_CHUNK_SCHEMA_VERSION},
                "facts": _facts_schema(),
            },
        },
    )


def _recover_fact_chunk(
    document: dict[str, Any],
    *,
    defer_invalid_facts: bool,
) -> tuple[dict[str, Any], list[str], list[str]] | None:
    recovered = copy.deepcopy(document)
    if recovered.get("schema_version") != FACT_CHUNK_SCHEMA_VERSION:
        return None
    facts = recovered.get("facts")
    if not isinstance(facts, dict):
        return None

    reasons: list[str] = []
    retry_segment_ids: list[str] = []
    for index, (fact_id, raw_fact) in enumerate(list(facts.items()), start=1):
        if not isinstance(raw_fact, Mapping):
            return None
        try:
            _validate_schema(dict(raw_fact), _fact_schema(), f"$.facts[{index}]")
            continue
        except InvalidClinicalLlmOutput:
            pass

        candidate_ref = raw_fact.get("candidate_ref")
        fact_type = raw_fact.get("type")
        term_like_fact = fact_type in {"MATCHED_TERM", "TERM"}
        missing_candidate_ref = (
            "candidate_ref" not in raw_fact
            or candidate_ref is None
            or (isinstance(candidate_ref, str) and not candidate_ref.strip())
        )
        text = raw_fact.get("text")
        downgraded = copy.deepcopy(dict(raw_fact))
        downgraded.pop("candidate_ref", None)
        downgraded["type"] = "UNMATCHED_TERM"
        if (
            term_like_fact
            and missing_candidate_ref
            and isinstance(text, str)
            and text.strip()
        ):
            try:
                _validate_schema(downgraded, _fact_schema(), f"$.facts[{index}]")
            except InvalidClinicalLlmOutput:
                pass
            else:
                facts[fact_id] = downgraded
                reasons.append(
                    f"fact[{index}]: TEXT_FACT_WITHOUT_CANDIDATE_REF_DOWNGRADED"
                )
                continue

        if (
            defer_invalid_facts
            and term_like_fact
            and missing_candidate_ref
        ):
            raw_segments = raw_fact.get("segments")
            if (
                not isinstance(raw_segments, list)
                or not raw_segments
                or any(
                    not isinstance(segment_id, str) or not segment_id.strip()
                    for segment_id in raw_segments
                )
            ):
                return None
            del facts[fact_id]
            for segment_id in raw_segments:
                if segment_id not in retry_segment_ids:
                    retry_segment_ids.append(segment_id)
            reasons.append(f"fact[{index}]: INVALID_FACT_DEFERRED_TO_SEGMENT_RETRY")
            continue
        return None

    if not reasons:
        return None
    try:
        _validate_schema(
            recovered,
            fact_chunk_response_format()["json_schema"]["schema"],
        )
    except InvalidClinicalLlmOutput:
        return None
    return recovered, reasons, retry_segment_ids


def recover_fact_chunk_response(
    document: dict[str, Any],
) -> tuple[dict[str, Any], list[str]] | None:
    """Downgrade schema-invalid textual facts without inventing a candidate."""

    result = _recover_fact_chunk(document, defer_invalid_facts=False)
    if result is None:
        return None
    recovered, reasons, _ = result
    return recovered, reasons


def recover_partial_fact_chunk_response(
    document: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]] | None:
    """Preserve valid facts and defer malformed fact segments to a smaller retry."""

    return _recover_fact_chunk(document, defer_invalid_facts=True)


def field_response_format(
    field_ids: Iterable[str] = CANONICAL_FIELD_IDS,
) -> dict[str, Any]:
    allowed = tuple(field_id for field_id in field_ids if field_id in CANONICAL_FIELD_IDS)
    return _format(
        "clinical_record_compact_fields_v1",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema_version", "fields"],
            "properties": {
                "schema_version": {"const": FIELD_SCHEMA_VERSION},
                "fields": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {field_id: _field_schema() for field_id in allowed},
                },
            },
        },
    )


def to_legacy_validation_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Add only structural v3 envelopes so the mature validators remain shared."""

    raw_facts = document.get("facts")
    raw_facts = raw_facts if isinstance(raw_facts, Mapping) else {}
    facts: dict[str, Any] = {}
    for fact_id, raw_fact in raw_facts.items():
        if not isinstance(raw_fact, Mapping):
            facts[str(fact_id)] = copy.deepcopy(raw_fact)
            continue
        fact = copy.deepcopy(dict(raw_fact))
        if fact.get("type") == "UNMATCHED_TERM":
            fact["review_code"] = "NO_MATCH"
        facts[str(fact_id)] = fact

    sparse_fields = document.get("fields")
    sparse_fields = sparse_fields if isinstance(sparse_fields, Mapping) else {}
    fields: dict[str, Any] = {}
    for field_id in CANONICAL_FIELD_IDS:
        raw_field = sparse_fields.get(field_id)
        if isinstance(raw_field, Mapping):
            fields[field_id] = {
                "generation_status": "GENERATED",
                "text": copy.deepcopy(raw_field.get("text")),
                "fact_refs": copy.deepcopy(raw_field.get("fact_refs", [])),
            }
        else:
            fields[field_id] = {
                "generation_status": "NOT_MENTIONED",
                "text": None,
                "fact_refs": [],
            }
    return {
        "schema_version": "clinical-record-compact-v3",
        "facts": facts,
        "fields": fields,
    }


def _max_status(current: str, incoming: str) -> str:
    return incoming if _STATUS_RANK.get(incoming, 0) > _STATUS_RANK.get(current, 0) else current


def validate_lean_record(
    document: Mapping[str, Any],
    *,
    segment_ids: Iterable[str],
    candidate_snapshots: Mapping[str, Mapping[str, Any]],
    failed_segment_ids: Iterable[str] = (),
    impacted_field_ids: Iterable[str] = (),
    technical_status: str = "completed",
) -> dict[str, Any]:
    """Validate Lean output without changing its clinical text or assertions."""

    normalized = to_legacy_validation_document(document)
    validation = validate_compact_record(
        normalized,
        segment_ids=segment_ids,
        candidate_snapshots=candidate_snapshots,
    )
    validation["schema_version"] = "clinical-record-compact-v3.1-validation-v1"
    facts = document.get("facts") if isinstance(document.get("facts"), Mapping) else {}
    fields = document.get("fields") if isinstance(document.get("fields"), Mapping) else {}
    referenced: set[str] = set()
    for field in fields.values():
        if isinstance(field, Mapping):
            referenced.update(str(value) for value in field.get("fact_refs", []))

    extra_issues: list[dict[str, Any]] = []
    try:
        _validate_schema(
            dict(document),
            lean_record_response_format()["json_schema"]["schema"],
        )
    except (InvalidClinicalLlmOutput, TypeError, ValueError) as error:
        extra_issues.append(
            {
                "issue_code": "INVALID_LEAN_CONTRACT",
                "severity": "BLOCK",
                "message": str(error),
                "fact_id": None,
                "field_ids": [],
            }
        )
    for fact_id in facts:
        fact = facts.get(fact_id)
        supersedes = (
            fact.get("supersedes_fact_id")
            if isinstance(fact, Mapping)
            else None
        )
        if supersedes is not None and str(supersedes) not in facts:
            affected_fields = [
                str(field_id)
                for field_id, field in fields.items()
                if isinstance(field, Mapping)
                and str(fact_id) in {
                    str(value) for value in field.get("fact_refs", [])
                }
            ]
            extra_issues.append(
                {
                    "issue_code": "INVALID_SUPERSEDES_FACT_REFERENCE",
                    "severity": "BLOCK",
                    "message": (
                        "supersedes_fact_id does not reference an existing fact"
                    ),
                    "fact_id": str(fact_id),
                    "field_ids": affected_fields,
                }
            )
        if str(fact_id) not in referenced:
            extra_issues.append(
                {
                    "issue_code": "UNASSIGNED_FACT",
                    "severity": "REVIEW_REQUIRED",
                    "message": "generated fact is not assigned to a clinical field",
                    "fact_id": str(fact_id),
                    "field_ids": [],
                }
            )
    for field_id in fields:
        if field_id not in CANONICAL_FIELD_IDS:
            extra_issues.append(
                {
                    "issue_code": "INVALID_FIELD_CONTRACT",
                    "severity": "BLOCK",
                    "message": f"compact record contains an unknown field: {field_id}",
                    "fact_id": None,
                    "field_ids": [str(field_id)],
                }
            )

    failed = sorted({str(value) for value in failed_segment_ids if str(value)})
    impacted = sorted(
        {str(value) for value in impacted_field_ids if value in CANONICAL_FIELD_IDS}
    )
    if failed:
        extra_issues.append(
            {
                "issue_code": "CHUNK_GENERATION_FAILED",
                "severity": "REVIEW_REQUIRED",
                "message": "one or more source segments could not be generated",
                "fact_id": None,
                "field_ids": impacted,
                "segment_ids": failed,
            }
        )
    if impacted:
        extra_issues.append(
            {
                "issue_code": "FIELD_GENERATION_FAILED",
                "severity": "REVIEW_REQUIRED",
                "message": "one or more clinical fields could not be generated",
                "fact_id": None,
                "field_ids": impacted,
            }
        )

    validation["issues"].extend(extra_issues)
    fact_statuses = validation.setdefault("fact_statuses", {})
    field_statuses = validation.setdefault("field_statuses", {})
    for issue in extra_issues:
        severity = str(issue.get("severity") or "PASS")
        fact_id = issue.get("fact_id")
        if fact_id in fact_statuses:
            fact_statuses[fact_id] = _max_status(fact_statuses[fact_id], severity)
        for field_id in issue.get("field_ids", []):
            if field_id in field_statuses:
                field_statuses[field_id] = _max_status(field_statuses[field_id], severity)
        validation["status"] = _max_status(validation.get("status", "PASS"), severity)
    if technical_status in {"partial", "failed"}:
        validation["processing_status"] = technical_status
    validation["summary"]["issue_count"] = len(validation["issues"])
    validation["summary"]["fact_count"] = len(facts)
    validation["summary"]["generated_field_count"] = len(fields)
    validation["summary"]["failed_segment_count"] = len(failed)
    return validation


def merge_chunk_facts(
    chunks: Iterable[tuple[int, Mapping[str, Any]]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Rename IDs and collapse only byte-for-byte equivalent facts."""

    merged: dict[str, Any] = {}
    audit: list[dict[str, Any]] = []
    signatures: dict[str, str] = {}
    for chunk_index, chunk_facts in chunks:
        id_map: dict[str, str] = {}
        for position, (old_id, raw_fact) in enumerate(chunk_facts.items(), start=1):
            id_map[str(old_id)] = f"c{chunk_index:02d}_f{position:03d}"
        for old_id, raw_fact in chunk_facts.items():
            fact = copy.deepcopy(dict(raw_fact)) if isinstance(raw_fact, Mapping) else copy.deepcopy(raw_fact)
            if isinstance(fact, dict) and fact.get("supersedes_fact_id") in id_map:
                fact["supersedes_fact_id"] = id_map[str(fact["supersedes_fact_id"])]
            signature = json.dumps(fact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            new_id = id_map[str(old_id)]
            kept_id = signatures.get(signature)
            if kept_id is None:
                kept_id = new_id
                signatures[signature] = kept_id
                merged[kept_id] = fact
            audit.append(
                {
                    "chunk_id": f"chunk_{chunk_index:02d}",
                    "original_fact_id": str(old_id),
                    "fact_id": kept_id,
                }
            )
    return merged, audit


def remap_field_fact_refs(
    fields: Mapping[str, Any],
    id_map: Mapping[str, str],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field_id, raw_field in fields.items():
        if not isinstance(raw_field, Mapping):
            continue
        field = copy.deepcopy(dict(raw_field))
        field["fact_refs"] = [
            id_map.get(str(fact_id), str(fact_id))
            for fact_id in field.get("fact_refs", [])
        ]
        result[str(field_id)] = field
    return result


__all__ = [
    "FACT_CHUNK_SCHEMA_VERSION",
    "FIELD_GROUPS",
    "FIELD_SCHEMA_VERSION",
    "MAX_CHUNKS",
    "MAX_LOGICAL_LLM_CALLS",
    "MAX_SEGMENTS_PER_CHUNK",
    "MAX_SPLIT_DEPTH",
    "PROMPT_VERSION",
    "SCHEMA_VERSION",
    "fact_chunk_response_format",
    "field_response_format",
    "lean_record_response_format",
    "merge_chunk_facts",
    "minimal_candidate_projection",
    "recover_fact_chunk_response",
    "recover_partial_fact_chunk_response",
    "to_legacy_validation_document",
    "validate_lean_record",
]
