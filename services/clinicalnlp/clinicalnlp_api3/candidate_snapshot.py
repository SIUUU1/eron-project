from __future__ import annotations

import copy
import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping


SNAPSHOT_SCHEMA_VERSION = "clinical-candidate-snapshot-v1"
_REFERENCE_PREFIX = "cr_"
_HASH_LENGTH = 32
_REQUIRED_KEYS = frozenset(
    {
        "request_id",
        "query_id",
        "segment_id",
        "source_span",
        "source_start",
        "source_end",
        "translated_query",
        "candidate_id",
        "canonical",
        "concept_id",
        "semantic_types",
        "retrieval_source",
        "retrieval_score",
        "rank",
        "versions",
        "created_at",
    }
)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _snapshot_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in snapshot.items()
        if key not in {"candidate_ref", "snapshot_sha256"}
    }


def _validate_snapshot_payload(payload: Mapping[str, Any]) -> None:
    missing = sorted(_REQUIRED_KEYS - set(payload))
    if missing:
        raise ValueError(
            "candidate snapshot is missing required keys: " + ", ".join(missing)
        )
    if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("candidate snapshot has an unsupported schema_version")

    for key in (
        "request_id",
        "query_id",
        "segment_id",
        "source_span",
        "translated_query",
        "candidate_id",
        "canonical",
        "retrieval_source",
        "created_at",
    ):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            raise ValueError(f"candidate snapshot {key} must be a non-empty string")

    concept_id = payload.get("concept_id")
    if concept_id is not None and (
        not isinstance(concept_id, str) or not concept_id.strip()
    ):
        raise ValueError("candidate snapshot concept_id must be null or a string")

    start = payload.get("source_start")
    end = payload.get("source_end")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end <= start
    ):
        raise ValueError("candidate snapshot source offsets are invalid")

    semantic_types = payload.get("semantic_types")
    if not isinstance(semantic_types, list) or any(
        not isinstance(value, str) or not value.strip()
        for value in semantic_types
    ):
        raise ValueError("candidate snapshot semantic_types must be a string array")

    score = payload.get("retrieval_score")
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(float(score))
        or not 0.0 <= float(score) <= 1.0
    ):
        raise ValueError("candidate snapshot retrieval_score must be between 0 and 1")

    rank = payload.get("rank")
    if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
        raise ValueError("candidate snapshot rank must be a positive integer")

    versions = payload.get("versions")
    if not isinstance(versions, dict) or not versions or any(
        not isinstance(key, str)
        or not key
        or not isinstance(value, str)
        or not value
        for key, value in versions.items()
    ):
        raise ValueError("candidate snapshot versions must be a string map")


def seal_candidate_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return a tamper-evident snapshot of one retrieval result.

    The reference identifies the search result that Gemma actually saw. It is
    not a live terminology lookup key and must remain attached to this exact
    payload for audit and replay.
    """

    if not isinstance(snapshot, Mapping):
        raise ValueError("candidate snapshot must be an object")
    payload = _snapshot_payload(snapshot)
    payload.setdefault("schema_version", SNAPSHOT_SCHEMA_VERSION)
    _validate_snapshot_payload(payload)
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    sealed = copy.deepcopy(payload)
    sealed["candidate_ref"] = f"{_REFERENCE_PREFIX}{digest[:_HASH_LENGTH]}"
    sealed["snapshot_sha256"] = digest
    return sealed


def verify_candidate_snapshot(snapshot: Mapping[str, Any]) -> bool:
    """Return whether a snapshot still matches its immutable reference."""

    if not isinstance(snapshot, Mapping):
        return False
    candidate_ref = snapshot.get("candidate_ref")
    snapshot_sha256 = snapshot.get("snapshot_sha256")
    if (
        not isinstance(candidate_ref, str)
        or not candidate_ref.startswith(_REFERENCE_PREFIX)
        or not isinstance(snapshot_sha256, str)
    ):
        return False
    try:
        payload = _snapshot_payload(snapshot)
        _validate_snapshot_payload(payload)
    except ValueError:
        return False
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return (
        snapshot_sha256 == digest
        and candidate_ref == f"{_REFERENCE_PREFIX}{digest[:_HASH_LENGTH]}"
    )


def snapshots_from_api3_document(
    document: Mapping[str, Any],
    *,
    request_id: str,
    created_at: str | None = None,
    max_annotations: int = 16,
    max_candidates_per_annotation: int = 3,
) -> dict[str, dict[str, Any]]:
    """Seal candidates only when RAW offsets and a data version are auditable."""

    if not isinstance(document, Mapping):
        return {}
    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    snapshots: dict[str, dict[str, Any]] = {}
    annotation_count = 0
    for segment in document.get("segments", []):
        if not isinstance(segment, Mapping):
            continue
        segment_id = segment.get("id")
        raw_text = segment.get("raw_text")
        if not isinstance(segment_id, str) or not isinstance(raw_text, str):
            continue
        for position, annotation in enumerate(segment.get("annotations", [])):
            if annotation_count >= max_annotations:
                return snapshots
            if not isinstance(annotation, Mapping):
                continue
            span = annotation.get("source_span")
            if not isinstance(span, Mapping):
                continue
            source_span = span.get("text")
            start = span.get("start_char")
            end = span.get("end_char")
            if (
                not isinstance(source_span, str)
                or not source_span
                or not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or start < 0
                or end <= start
                or end > len(raw_text)
                or raw_text[start:end] != source_span
            ):
                continue
            candidates = annotation.get("candidates")
            if not isinstance(candidates, list):
                continue
            annotation_count += 1
            search_terms = annotation.get("search_terms_en")
            translated_query = source_span
            if isinstance(search_terms, list):
                translated_query = next(
                    (
                        value.strip()
                        for value in search_terms
                        if isinstance(value, str) and value.strip()
                    ),
                    source_span,
                )
            for rank, candidate in enumerate(
                candidates[:max_candidates_per_annotation], start=1
            ):
                if not isinstance(candidate, Mapping):
                    continue
                candidate_id = candidate.get("entity_id")
                dictionary_version = candidate.get("dictionary_version")
                canonical = candidate.get("canonical_en") or candidate.get(
                    "canonical_ko"
                )
                score = candidate.get("retrieval_score")
                source = candidate.get("match_type")
                if (
                    not isinstance(candidate_id, str)
                    or not candidate_id
                    or not isinstance(dictionary_version, str)
                    or not dictionary_version
                    or not isinstance(canonical, str)
                    or not canonical
                    or not isinstance(source, str)
                    or not source
                    or isinstance(score, bool)
                    or not isinstance(score, (int, float))
                ):
                    continue
                provenance = candidate.get("provenance")
                provenance = provenance if isinstance(provenance, Mapping) else {}
                semantic_types = provenance.get("semantic_types", [])
                if not isinstance(semantic_types, list):
                    semantic_types = []
                try:
                    sealed = seal_candidate_snapshot(
                        {
                            "schema_version": SNAPSHOT_SCHEMA_VERSION,
                            "request_id": request_id,
                            "query_id": f"{segment_id}:a{position}:c{rank}",
                            "segment_id": segment_id,
                            "source_span": source_span,
                            "source_start": start,
                            "source_end": end,
                            "translated_query": translated_query,
                            "candidate_id": candidate_id,
                            "canonical": canonical,
                            "concept_id": provenance.get("cui") or candidate_id,
                            "semantic_types": [
                                value
                                for value in semantic_types
                                if isinstance(value, str) and value.strip()
                            ],
                            "retrieval_source": source,
                            "retrieval_score": float(score),
                            "rank": rank,
                            "versions": {"dictionary": dictionary_version},
                            "created_at": timestamp,
                        }
                    )
                except ValueError:
                    continue
                snapshots[sealed["candidate_ref"]] = sealed
    return snapshots


__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "seal_candidate_snapshot",
    "snapshots_from_api3_document",
    "verify_candidate_snapshot",
]
