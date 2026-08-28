from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .annotations import (
    build_annotations,
    detect_numeric_annotations,
    limit_document_medical_candidates,
)
from .contracts import validate_whisper_payload
from .correction import apply_safe_corrections
from .preservation import preservation_violations
from .query_expansion import (
    retrieve_with_query_expansion,
    unresolved_expansion_annotations,
)


SCHEMA_VERSION = "clinical-stt-correction-v1"


def run_api3(
    whisper_payload: dict[str, Any],
    *,
    retriever: Any | None = None,
    max_candidates_per_span: int = 2,
    query_expansion: dict[str, Any] | None = None,
    pre_retrieved_candidates: dict[str, list[dict[str, Any]]] | None = None,
    resolved_candidates_by_segment: dict[Any, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Create an API3 document while preserving the Whisper evidence boundary."""
    segments = validate_whisper_payload(whisper_payload)
    effective_query_expansion = (
        {
            **query_expansion,
            "items": list(query_expansion.get("items", [])),
        }
        if isinstance(query_expansion, dict)
        else None
    )

    output_segments = []
    review_items: list[dict[str, Any]] = []
    processing_review_required = False
    for segment in segments:
        dialogue_context = segments
        segment_status = "completed"
        try:
            if resolved_candidates_by_segment is not None:
                candidates = list(
                    resolved_candidates_by_segment.get(segment["id"], [])
                )
                expansion_failures = []
            elif retriever is None:
                candidates = []
                expansion_failures = []
            else:
                (
                    candidates,
                    expansion_failures,
                    matched_translation_items,
                ) = retrieve_with_query_expansion(
                    retriever=retriever,
                    segment=segment,
                    context=dialogue_context,
                    expansion=effective_query_expansion,
                    base_candidates=(
                        pre_retrieved_candidates[segment["id"]]
                        if pre_retrieved_candidates is not None
                        and segment["id"] in pre_retrieved_candidates
                        else None
                    ),
                )
                if effective_query_expansion is not None:
                    effective_query_expansion["items"].extend(
                        matched_translation_items
                    )
            for failure in expansion_failures:
                processing_review_required = True
                review_items.append(
                    {
                        "segment_id": segment["id"],
                        "type": "query_expansion_retrieval_failed",
                        "reason": failure["error_code"],
                    }
                )
        except Exception as error:
            candidates = []
            segment_status = "needs_review"
            processing_review_required = True
            review_items.append(
                {
                    "segment_id": segment["id"],
                    "type": "retrieval_failed",
                    "reason": type(error).__name__,
                }
            )
        corrected_text, corrections, rejected = apply_safe_corrections(
            segment["text"], candidates
        )
        for item in rejected:
            review_items.append({"segment_id": segment["id"], **item})
        if rejected:
            processing_review_required = True
        violations = preservation_violations(segment["text"], corrected_text)
        if violations:
            corrected_text = segment["text"]
            corrections = []
            segment_status = "needs_review"
            processing_review_required = True
            review_items.append(
                {
                    "segment_id": segment["id"],
                    "type": "preservation_validation_failed",
                    "violations": violations,
                }
            )
        annotations = build_annotations(
                segment["text"],
                candidates,
                max_candidates_per_span=max_candidates_per_span,
            )
        annotations += unresolved_expansion_annotations(
            segment=segment,
            expansion=query_expansion,
            existing_annotations=annotations,
        )
        annotations = sorted(
            annotations + detect_numeric_annotations(segment["text"]),
            key=lambda item: item["source_span"]["start_char"],
        )
        output_segments.append(
            {
                "id": segment["id"],
                "start": segment["start"],
                "end": segment["end"],
                "raw_text": segment["text"],
                "corrected_text": corrected_text,
                "processing_status": segment_status,
                "corrections": corrections,
                "annotations": annotations,
            }
        )

    limit_document_medical_candidates(output_segments)
    for output_segment in output_segments:
        for annotation_index, annotation in enumerate(output_segment["annotations"]):
            if not annotation["needs_review"]:
                continue
            review_items.append(
                {
                    "segment_id": output_segment["id"],
                    "type": "annotation_needs_review",
                    "annotation_index": annotation_index,
                    "annotation_type": annotation["type"],
                    "source_span": dict(annotation["source_span"]),
                    "candidate_count": len(annotation["candidates"]),
                }
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "segments": output_segments,
        "review_items": review_items,
        "query_expansion": effective_query_expansion
        if isinstance(effective_query_expansion, dict)
        else {
            "status": "disabled",
            "fallback_used": False,
            "items": [],
        },
        "metadata": {
            "processing_status": (
                "partial" if processing_review_required else "completed"
            ),
            "alias_db_version": getattr(retriever, "alias_db_version", 0),
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    }

