"""Run one ClinicalNLP draft while auditing actual vector fallback queries."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterator, Sequence


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from clinicalnlp_api3.medical_vector_repository import (  # noqa: E402
    PostgresMedicalVectorRepository,
    VectorIdentityBatch,
    _tokens,
)
from clinicalnlp_api3.service import (  # noqa: E402
    ServiceSettings,
    build_service_runtime,
)


class VectorFallbackTrace:
    """Collect bounded query metadata without exposing text by default."""

    def __init__(self, *, full_trace: bool = False) -> None:
        self._full_trace = bool(full_trace)
        self._batches: list[dict[str, Any]] = []
        self._events: list[dict[str, Any]] = []

    @staticmethod
    def _fingerprint(query_text: str) -> str:
        normalized = query_text.strip().casefold().encode("utf-8")
        return hashlib.sha256(normalized).hexdigest()

    def record_batch(
        self,
        requests: Sequence[tuple[str, frozenset[str]]],
        *,
        skip_collections_by_index: dict[int, frozenset[str]] | None,
        batch: VectorIdentityBatch,
    ) -> None:
        batch_index = len(self._batches)
        self._batches.append(
            {
                "batch_index": batch_index,
                "elapsed_ms": batch.elapsed_ms,
                "statement_count": batch.statement_count,
                "query_count": len(requests),
                "collection_elapsed_ms": dict(batch.collection_elapsed_ms),
            }
        )
        skipped = skip_collections_by_index or {}
        for index, (query_text, requested_collections) in enumerate(requests):
            requested = frozenset(requested_collections)
            effective = requested - skipped.get(index, frozenset())
            identities = (
                batch.identities[index]
                if index < len(batch.identities)
                else ()
            )
            counts = Counter(identity.collection for identity in identities)
            event: dict[str, Any] = {
                "batch_index": batch_index,
                "query_index": index,
                "query_sha256": self._fingerprint(query_text),
                "character_count": len(query_text),
                "token_count": len(_tokens(query_text)),
                "requested_collections": sorted(requested),
                "skipped_collections": sorted(
                    skipped.get(index, frozenset())
                ),
                "effective_collections": sorted(effective),
                "candidate_count": len(identities),
                "candidate_count_by_collection": dict(sorted(counts.items())),
                "empty": not identities,
                "drug_empty": (
                    "drug_terms" in effective
                    and counts.get("drug_terms", 0) == 0
                ),
            }
            if self._full_trace:
                event["query_text"] = query_text
                event["candidate_ids"] = [
                    f"{identity.collection}:{identity.entity_id}"
                    for identity in identities
                ]
            self._events.append(event)

    def to_dict(self) -> dict[str, Any]:
        drug_events = [
            event
            for event in self._events
            if "drug_terms" in event["effective_collections"]
        ]
        return {
            "full_trace": self._full_trace,
            "batch_count": len(self._batches),
            "query_event_count": len(self._events),
            "unique_query_count": len({
                event["query_sha256"] for event in self._events
            }),
            "drug_query_count": len(drug_events),
            "empty_drug_query_count": sum(
                bool(event["drug_empty"]) for event in drug_events
            ),
            "batches": list(self._batches),
            "events": list(self._events),
        }


@contextmanager
def _capture_postgres_vector_fallbacks(
    trace: VectorFallbackTrace,
) -> Iterator[None]:
    original = PostgresMedicalVectorRepository.search_many

    def observed_search_many(
        repository: PostgresMedicalVectorRepository,
        requests: Sequence[tuple[str, frozenset[str]]],
        *,
        limit: int,
        skip_collections_by_index: dict[int, frozenset[str]] | None = None,
    ) -> VectorIdentityBatch:
        normalized_requests = tuple(requests)
        batch = original(
            repository,
            normalized_requests,
            limit=limit,
            skip_collections_by_index=skip_collections_by_index,
        )
        trace.record_batch(
            normalized_requests,
            skip_collections_by_index=skip_collections_by_index,
            batch=batch,
        )
        return batch

    PostgresMedicalVectorRepository.search_many = observed_search_many
    try:
        yield
    finally:
        PostgresMedicalVectorRepository.search_many = original


def _input_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("input must contain one Whisper JSON object")
    return payload


def _error_summary(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "stage": item.get("stage"),
            "code": item.get("code"),
        }
        for item in result.get("errors", [])
        if isinstance(item, dict)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one local ClinicalNLP workflow and export vector fallback "
            "diagnostics. Query text and candidate IDs are redacted unless "
            "--full-trace is explicitly supplied."
        )
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument(
        "--mode",
        choices=("compare", "primary", "legacy", "lean_shadow", "lean_primary"),
        default=None,
    )
    parser.add_argument("--full-trace", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    input_bytes = args.input.read_bytes()
    payload = _input_payload(args.input)
    environment = dict(os.environ)
    if args.mode is not None:
        environment["CLINICALNLP_COMPACT_V3_MODE"] = args.mode
    settings = ServiceSettings.from_mapping(environment)
    trace = VectorFallbackTrace(full_trace=args.full_trace)

    started = time.perf_counter()
    with _capture_postgres_vector_fallbacks(trace):
        bundle = build_service_runtime(settings)
        try:
            result = bundle.runtime.generate_draft(payload)
        finally:
            bundle.close()
    output = {
        "schema_version": "clinical-vector-fallback-evaluation-v1",
        "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
        "compact_v3_mode": settings.compact_v3_mode,
        "workflow": {
            "wall_ms": round((time.perf_counter() - started) * 1000, 3),
            "processing_status": result.get("processing_status"),
            "errors": _error_summary(result),
            "telemetry": result.get("telemetry", {}),
        },
        "vector_fallback": trace.to_dict(),
    }
    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
