from __future__ import annotations

import copy
import re
from collections import defaultdict
from typing import Any, Iterable, Mapping

from .candidate_snapshot import verify_candidate_snapshot


SCHEMA_VERSION = "clinical-record-compact-v3"
VALIDATION_SCHEMA_VERSION = "clinical-record-compact-validation-v1"

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
    "impression",
    "treatment_plan",
    "outcome",
)
FACT_TYPES = frozenset(
    {"MATCHED_TERM", "UNMATCHED_TERM", "NARRATIVE", "MEASUREMENT"}
)
ASSERTION_VALUES = frozenset({"PRESENT", "DENIED", "UNCERTAIN"})
GENERATION_STATUS_VALUES = frozenset(
    {"GENERATED", "NOT_MENTIONED", "FAILED"}
)
VALIDATION_STATUS_VALUES = ("PASS", "REVIEW_REQUIRED", "BLOCK")

MAX_FACTS = 128
MAX_SEGMENTS_PER_FACT = 16
MAX_FACT_REFS_PER_FIELD = 128
MAX_FIELD_TEXT_LENGTH = 4096

_STATUS_RANK = {value: index for index, value in enumerate(VALIDATION_STATUS_VALUES)}


def _unique_strings(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str)))


def _segment_schema(segment_ids: Iterable[str]) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    values = _unique_strings(segment_ids)
    if values:
        schema["enum"] = values
    return schema


def _candidate_ref_schema(candidate_refs: Iterable[str]) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    values = _unique_strings(candidate_refs)
    if values:
        schema["enum"] = values
    return schema


def _fact_schemas(
    segment_ids: Iterable[str],
    candidate_refs: Iterable[str],
) -> list[dict[str, Any]]:
    segments = {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_SEGMENTS_PER_FACT,
        "uniqueItems": True,
        "items": _segment_schema(segment_ids),
    }
    assertion = {"type": "string", "enum": sorted(ASSERTION_VALUES)}
    supersedes = {"type": "string", "minLength": 1}
    variants: list[dict[str, Any]] = []

    def variant(
        fact_type: str,
        required: list[str],
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "assertion", "segments", *required],
            "properties": {
                "type": {"const": fact_type},
                "assertion": assertion,
                "segments": segments,
                "supersedes_fact_id": supersedes,
                **extra,
            },
        }

    variants.append(
        variant(
            "MATCHED_TERM",
            ["candidate_ref"],
            {"candidate_ref": _candidate_ref_schema(candidate_refs)},
        )
    )
    variants.append(
        variant(
            "UNMATCHED_TERM",
            ["text", "review_code"],
            {
                "text": {"type": "string", "minLength": 1, "maxLength": 512},
                "review_code": {"const": "NO_MATCH"},
            },
        )
    )
    variants.append(
        variant(
            "NARRATIVE",
            ["text"],
            {"text": {"type": "string", "minLength": 1, "maxLength": 2048}},
        )
    )
    variants.append(
        variant(
            "MEASUREMENT",
            ["values"],
            {
                "values": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": {
                        "type": ["number", "string", "null"]
                    },
                }
            },
        )
    )
    return variants


def _field_schema() -> dict[str, Any]:
    common_properties = {
        "generation_status": {
            "type": "string",
            "enum": sorted(GENERATION_STATUS_VALUES),
        },
        "text": {"type": ["string", "null"], "maxLength": MAX_FIELD_TEXT_LENGTH},
        "fact_refs": {
            "type": "array",
            "maxItems": MAX_FACT_REFS_PER_FIELD,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "values": {"type": ["object", "null"]},
        "error_code": {"type": ["string", "null"]},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["generation_status", "text", "fact_refs"],
        "properties": common_properties,
    }


def compact_record_response_format(
    segment_ids: Iterable[str],
    candidate_refs: Iterable[str],
) -> dict[str, Any]:
    """Return the bounded Gemma contract for the common Compact v3 envelope.

    Field-specific value schemas intentionally remain open until each emergency
    record field contract is approved. Existing runtime output is not changed by
    this contract alone.
    """

    fields = {field_id: _field_schema() for field_id in CANONICAL_FIELD_IDS}
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "facts", "fields"],
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "facts": {
                "type": "object",
                "maxProperties": MAX_FACTS,
                "additionalProperties": {
                    "oneOf": _fact_schemas(segment_ids, candidate_refs)
                },
            },
            "fields": {
                "type": "object",
                "additionalProperties": False,
                "required": list(CANONICAL_FIELD_IDS),
                "properties": fields,
            },
        },
    }
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "clinical_record_compact_v3",
            "strict": True,
            "schema": schema,
        },
    }


def _status_max(*values: str) -> str:
    valid = [value for value in values if value in _STATUS_RANK]
    return max(valid, key=_STATUS_RANK.__getitem__) if valid else "PASS"


def _issue(
    code: str,
    severity: str,
    message: str,
    *,
    fact_id: str | None = None,
    field_ids: Iterable[str] = (),
    rule_id: str | None = None,
) -> dict[str, Any]:
    value = {
        "issue_code": code,
        "severity": severity,
        "message": message,
        "fact_id": fact_id,
        "field_ids": sorted(set(field_ids)),
    }
    if rule_id is not None:
        value["rule_id"] = rule_id
    return value


def _normalized_concept_text(value: Any) -> str:
    return "".join(re.findall(r"[0-9a-z가-힣]+", str(value or "").casefold()))


def _fact_concept_key(
    fact: Mapping[str, Any],
    candidate_snapshots: Mapping[str, Mapping[str, Any]],
) -> str | None:
    if fact.get("type") == "MATCHED_TERM":
        snapshot = candidate_snapshots.get(str(fact.get("candidate_ref") or ""))
        if not isinstance(snapshot, Mapping) or not verify_candidate_snapshot(snapshot):
            return None
        concept_id = snapshot.get("concept_id") or snapshot.get("candidate_id")
        return f"matched:{concept_id}" if concept_id else None
    if fact.get("type") == "UNMATCHED_TERM":
        normalized = _normalized_concept_text(fact.get("text"))
        return f"unmatched:{normalized}" if normalized else None
    return None


def _fact_shape_issues(
    fact_id: str,
    fact: Any,
    *,
    segment_ids: set[str],
    candidate_snapshots: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(fact, dict):
        return [
            _issue(
                "INVALID_FACT_CONTRACT",
                "BLOCK",
                "fact must be an object",
                fact_id=fact_id,
            )
        ]
    fact_type = fact.get("type")
    if fact_type not in FACT_TYPES:
        issues.append(
            _issue(
                "INVALID_FACT_CONTRACT",
                "BLOCK",
                "fact has an unsupported type",
                fact_id=fact_id,
            )
        )
    if fact.get("assertion") not in ASSERTION_VALUES:
        issues.append(
            _issue(
                "INVALID_FACT_CONTRACT",
                "BLOCK",
                "fact has an unsupported assertion",
                fact_id=fact_id,
            )
        )
    segments = fact.get("segments")
    if (
        not isinstance(segments, list)
        or not segments
        or len(segments) > MAX_SEGMENTS_PER_FACT
        or len(segments) != len(set(map(str, segments)))
    ):
        issues.append(
            _issue(
                "INVALID_FACT_CONTRACT",
                "BLOCK",
                "fact must reference one or more unique source segments",
                fact_id=fact_id,
            )
        )
        segments = []
    for segment_id in segments:
        if str(segment_id) not in segment_ids:
            issues.append(
                _issue(
                    "UNSUPPORTED_EVIDENCE_REFERENCE",
                    "BLOCK",
                    f"fact references an unknown segment: {segment_id}",
                    fact_id=fact_id,
                    rule_id="G01",
                )
            )

    if fact_type == "MATCHED_TERM":
        candidate_ref = fact.get("candidate_ref")
        snapshot = candidate_snapshots.get(str(candidate_ref or ""))
        if not isinstance(candidate_ref, str) or not isinstance(snapshot, Mapping):
            issues.append(
                _issue(
                    "INVALID_CANDIDATE_REF",
                    "REVIEW_REQUIRED",
                    "matched term references an unavailable candidate snapshot",
                    fact_id=fact_id,
                )
            )
        elif not verify_candidate_snapshot(snapshot):
            issues.append(
                _issue(
                    "INVALID_CANDIDATE_SNAPSHOT",
                    "BLOCK",
                    "candidate snapshot failed its immutability check",
                    fact_id=fact_id,
                )
            )
        elif str(snapshot.get("segment_id")) not in {str(value) for value in segments}:
            issues.append(
                _issue(
                    "CANDIDATE_EVIDENCE_MISMATCH",
                    "BLOCK",
                    "candidate snapshot is not grounded in a fact source segment",
                    fact_id=fact_id,
                    rule_id="G01",
                )
            )
    elif fact_type == "UNMATCHED_TERM":
        if not isinstance(fact.get("text"), str) or not fact["text"].strip():
            issues.append(
                _issue(
                    "INVALID_FACT_CONTRACT",
                    "BLOCK",
                    "unmatched term must preserve its source expression",
                    fact_id=fact_id,
                )
            )
        if fact.get("review_code") != "NO_MATCH":
            issues.append(
                _issue(
                    "INVALID_FACT_CONTRACT",
                    "BLOCK",
                    "unmatched term must use review_code NO_MATCH",
                    fact_id=fact_id,
                )
            )
    elif fact_type == "NARRATIVE":
        if not isinstance(fact.get("text"), str) or not fact["text"].strip():
            issues.append(
                _issue(
                    "INVALID_FACT_CONTRACT",
                    "BLOCK",
                    "narrative fact must contain text",
                    fact_id=fact_id,
                )
            )
    elif fact_type == "MEASUREMENT":
        if not isinstance(fact.get("values"), dict) or not fact["values"]:
            issues.append(
                _issue(
                    "INVALID_FACT_CONTRACT",
                    "BLOCK",
                    "measurement fact must contain structured values",
                    fact_id=fact_id,
                )
            )
    return issues


def _conflict_issues(
    facts: Mapping[str, Any],
    candidate_snapshots: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[tuple[str, Mapping[str, Any]]]] = defaultdict(list)
    for fact_id, fact in facts.items():
        if not isinstance(fact, Mapping):
            continue
        key = _fact_concept_key(fact, candidate_snapshots)
        if key:
            grouped[key].append((str(fact_id), fact))

    issues: list[dict[str, Any]] = []
    for items in grouped.values():
        superseded = {
            str(fact.get("supersedes_fact_id"))
            for _, fact in items
            if fact.get("supersedes_fact_id") is not None
        }
        active = [(fact_id, fact) for fact_id, fact in items if fact_id not in superseded]
        assertions = {str(fact.get("assertion")) for _, fact in active}
        if {"PRESENT", "DENIED"}.issubset(assertions):
            for fact_id, _ in active:
                issues.append(
                    _issue(
                        "CONFLICTING_ASSERTION",
                        "REVIEW_REQUIRED",
                        "the same clinical concept has unresolved opposing assertions",
                        fact_id=fact_id,
                        rule_id="G04",
                    )
                )
    return issues


def validate_compact_record(
    document: Mapping[str, Any],
    *,
    segment_ids: Iterable[str],
    candidate_snapshots: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate Compact v3 without rewriting or deleting model output.

    Issues are recorded at their narrowest fact/field location. Field and
    document statuses are deterministic severity projections of those issues.
    """

    known_segments = set(_unique_strings(segment_ids))
    snapshots = candidate_snapshots if isinstance(candidate_snapshots, Mapping) else {}
    issues: list[dict[str, Any]] = []
    if not isinstance(document, Mapping):
        issues.append(
            _issue(
                "INVALID_COMPACT_RECORD",
                "BLOCK",
                "compact record must be an object",
                field_ids=CANONICAL_FIELD_IDS,
            )
        )
        facts: Mapping[str, Any] = {}
        fields: Mapping[str, Any] = {}
    else:
        facts = document.get("facts") if isinstance(document.get("facts"), Mapping) else {}
        fields = document.get("fields") if isinstance(document.get("fields"), Mapping) else {}
        if document.get("schema_version") != SCHEMA_VERSION:
            issues.append(
                _issue(
                    "INVALID_COMPACT_RECORD",
                    "BLOCK",
                    "compact record has an unsupported schema_version",
                    field_ids=CANONICAL_FIELD_IDS,
                )
            )
        if not isinstance(document.get("facts"), Mapping) or len(facts) > MAX_FACTS:
            issues.append(
                _issue(
                    "INVALID_COMPACT_RECORD",
                    "BLOCK",
                    "facts must be an object within the configured limit",
                    field_ids=CANONICAL_FIELD_IDS,
                )
            )
        if not isinstance(document.get("fields"), Mapping):
            issues.append(
                _issue(
                    "INVALID_COMPACT_RECORD",
                    "BLOCK",
                    "fields must be an object",
                    field_ids=CANONICAL_FIELD_IDS,
                )
            )

    for fact_id, fact in facts.items():
        if not isinstance(fact_id, str) or not fact_id:
            issues.append(
                _issue(
                    "INVALID_FACT_CONTRACT",
                    "BLOCK",
                    "fact IDs must be non-empty strings",
                    fact_id=str(fact_id),
                )
            )
            continue
        issues.extend(
            _fact_shape_issues(
                fact_id,
                fact,
                segment_ids=known_segments,
                candidate_snapshots=snapshots,
            )
        )

    for fact_id, fact in facts.items():
        if not isinstance(fact, Mapping):
            continue
        supersedes = fact.get("supersedes_fact_id")
        if supersedes is not None and (
            not isinstance(supersedes, str)
            or supersedes == fact_id
            or supersedes not in facts
        ):
            issues.append(
                _issue(
                    "INVALID_FACT_RELATION",
                    "REVIEW_REQUIRED",
                    "supersedes_fact_id must reference another existing fact",
                    fact_id=str(fact_id),
                )
            )
        elif isinstance(supersedes, str):
            current_key = _fact_concept_key(fact, snapshots)
            previous = facts.get(supersedes)
            previous_key = (
                _fact_concept_key(previous, snapshots)
                if isinstance(previous, Mapping)
                else None
            )
            if current_key and previous_key and current_key != previous_key:
                issues.append(
                    _issue(
                        "INVALID_FACT_RELATION",
                        "REVIEW_REQUIRED",
                        "a fact may supersede only the same clinical concept",
                        fact_id=str(fact_id),
                    )
                )
    issues.extend(_conflict_issues(facts, snapshots))

    fact_to_fields: dict[str, set[str]] = defaultdict(set)
    field_statuses = {field_id: "PASS" for field_id in CANONICAL_FIELD_IDS}
    failed_fields = 0
    generated_fields = 0

    for field_id in CANONICAL_FIELD_IDS:
        field = fields.get(field_id)
        if not isinstance(field, Mapping):
            issues.append(
                _issue(
                    "INVALID_FIELD_CONTRACT",
                    "BLOCK",
                    "required field envelope is missing or invalid",
                    field_ids=[field_id],
                )
            )
            continue
        status = field.get("generation_status")
        text = field.get("text")
        refs = field.get("fact_refs")
        error_code = field.get("error_code")
        if status not in GENERATION_STATUS_VALUES:
            issues.append(
                _issue(
                    "INVALID_FIELD_CONTRACT",
                    "BLOCK",
                    "field has an unsupported generation_status",
                    field_ids=[field_id],
                )
            )
            continue
        if not isinstance(refs, list) or len(refs) > MAX_FACT_REFS_PER_FIELD:
            issues.append(
                _issue(
                    "INVALID_FIELD_CONTRACT",
                    "BLOCK",
                    "field fact_refs must be a bounded array",
                    field_ids=[field_id],
                )
            )
            refs = []
        elif len(refs) != len(set(map(str, refs))):
            issues.append(
                _issue(
                    "INVALID_FIELD_CONTRACT",
                    "BLOCK",
                    "field fact_refs must be unique",
                    field_ids=[field_id],
                )
            )

        valid_ref_count = 0
        for ref in refs:
            ref_id = str(ref)
            if ref_id not in facts:
                issues.append(
                    _issue(
                        "INVALID_FACT_REF",
                        "BLOCK",
                        f"field references an unknown fact: {ref_id}",
                        field_ids=[field_id],
                    )
                )
                continue
            valid_ref_count += 1
            fact_to_fields[ref_id].add(field_id)

        if status == "GENERATED":
            generated_fields += 1
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > MAX_FIELD_TEXT_LENGTH
            ):
                issues.append(
                    _issue(
                        "INVALID_FIELD_CONTRACT",
                        "BLOCK",
                        "generated field must contain bounded display text",
                        field_ids=[field_id],
                    )
                )
            if valid_ref_count == 0:
                issues.append(
                    _issue(
                        "UNSUPPORTED_FACT_REFERENCE",
                        "BLOCK",
                        "generated field text has no valid supporting fact",
                        field_ids=[field_id],
                        rule_id="G01",
                    )
                )
        elif status == "NOT_MENTIONED":
            if text not in (None, "") or refs or error_code not in (None, ""):
                issues.append(
                    _issue(
                        "INVALID_FIELD_CONTRACT",
                        "BLOCK",
                        "not-mentioned field must not contain text, facts, or errors",
                        field_ids=[field_id],
                    )
                )
        elif status == "FAILED":
            failed_fields += 1
            if text not in (None, "") or refs:
                issues.append(
                    _issue(
                        "INVALID_FIELD_CONTRACT",
                        "BLOCK",
                        "failed field must not masquerade as generated content",
                        field_ids=[field_id],
                    )
                )
            if not isinstance(error_code, str) or not error_code.strip():
                issues.append(
                    _issue(
                        "INVALID_FIELD_CONTRACT",
                        "BLOCK",
                        "failed field must contain an error_code",
                        field_ids=[field_id],
                    )
                )
            else:
                issues.append(
                    _issue(
                        "FIELD_GENERATION_FAILED",
                        "REVIEW_REQUIRED",
                        "field draft generation failed and requires manual completion",
                        field_ids=[field_id],
                    )
                )

    unknown_fields = sorted(set(fields) - set(CANONICAL_FIELD_IDS))
    for field_id in unknown_fields:
        issues.append(
            _issue(
                "INVALID_FIELD_CONTRACT",
                "BLOCK",
                f"compact record contains an unknown field: {field_id}",
                field_ids=[field_id],
            )
        )

    for issue in issues:
        fact_id = issue.get("fact_id")
        if fact_id and not issue.get("field_ids"):
            issue["field_ids"] = sorted(fact_to_fields.get(str(fact_id), set()))

    fact_statuses = {str(fact_id): "PASS" for fact_id in facts}
    for issue in issues:
        severity = str(issue.get("severity") or "PASS")
        fact_id = issue.get("fact_id")
        if fact_id in fact_statuses:
            fact_statuses[fact_id] = _status_max(
                fact_statuses[fact_id], severity
            )
        for field_id in issue.get("field_ids", []):
            if field_id in field_statuses:
                field_statuses[field_id] = _status_max(
                    field_statuses[field_id], severity
                )

    document_status = "PASS"
    for status in field_statuses.values():
        document_status = _status_max(document_status, status)
    for issue in issues:
        document_status = _status_max(
            document_status, str(issue.get("severity") or "PASS")
        )
    if failed_fields == len(CANONICAL_FIELD_IDS):
        processing_status = "failed"
    elif failed_fields:
        processing_status = "partial"
    else:
        processing_status = "completed"

    return {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "status": document_status,
        "processing_status": processing_status,
        "issues": copy.deepcopy(issues),
        "fact_statuses": fact_statuses,
        "field_statuses": field_statuses,
        "summary": {
            "fact_count": len(facts),
            "generated_field_count": generated_fields,
            "failed_field_count": failed_fields,
            "issue_count": len(issues),
        },
    }


__all__ = [
    "ASSERTION_VALUES",
    "CANONICAL_FIELD_IDS",
    "FACT_TYPES",
    "GENERATION_STATUS_VALUES",
    "SCHEMA_VERSION",
    "VALIDATION_SCHEMA_VERSION",
    "compact_record_response_format",
    "validate_compact_record",
]
