from __future__ import annotations

import copy
import hashlib
import json
import math
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


__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "seal_candidate_snapshot",
    "verify_candidate_snapshot",
]
