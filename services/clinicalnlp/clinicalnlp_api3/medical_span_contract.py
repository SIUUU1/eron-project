from __future__ import annotations

import math
from typing import Any, Iterable


MAX_SEGMENTS = 50
MAX_TOTAL_TRANSLATED_CHARS = 20_000


def _compact_text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def normalize_translated_segments(payload: Any) -> list[dict[str, str]]:
    """Validate the bounded translated input shared by clinical and A/B lanes."""

    candidates: Any = payload
    if isinstance(payload, dict):
        query_expansion = payload.get("query_expansion")
        if isinstance(query_expansion, dict):
            candidates = query_expansion.get("translated_segments")
        elif isinstance(payload.get("translated_segments"), list):
            candidates = payload["translated_segments"]
        elif isinstance(payload.get("segments"), list):
            candidates = payload["segments"]
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("translated_segments must be a non-empty array")
    if len(candidates) > MAX_SEGMENTS:
        raise ValueError(f"translated_segments must contain at most {MAX_SEGMENTS} items")

    segments: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    total_chars = 0
    for item in candidates:
        if not isinstance(item, dict):
            raise ValueError("each translated segment must be an object")
        segment_id = item.get("segment_id") or item.get("id")
        text = item.get("translated_text_en") or item.get("translated_text")
        if not isinstance(segment_id, str) or not segment_id.strip():
            raise ValueError("each translated segment must have a non-empty segment_id")
        segment_id = segment_id.strip()
        if segment_id in seen_ids:
            raise ValueError("translated segment IDs must be unique")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(
                "each translated segment must have non-empty translated_text_en"
            )
        compact_text = " ".join(text.split())
        total_chars += len(compact_text)
        if total_chars > MAX_TOTAL_TRANSLATED_CHARS:
            raise ValueError("translated segment text exceeds the character limit")
        seen_ids.add(segment_id)
        segments.append(
            {
                "segment_id": segment_id,
                "translated_text_en": compact_text,
            }
        )
    return segments


def validate_scispacy_spans(
    translated_segments: list[dict[str, Any]],
    extracted_spans: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only linker spans traceable to an exact translated substring."""

    text_by_id = {
        item.get("segment_id"): item.get("translated_text_en")
        for item in translated_segments
        if isinstance(item, dict)
        and isinstance(item.get("segment_id"), str)
        and isinstance(item.get("translated_text_en"), str)
    }
    valid: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for span in extracted_spans:
        if not isinstance(span, dict):
            continue
        segment_id = span.get("segment_id")
        text = _compact_text(span.get("text"))
        start = span.get("start_char")
        end = span.get("end_char")
        source = text_by_id.get(segment_id)
        if (
            not isinstance(source, str)
            or not text
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
            or end > len(source)
            or source[start:end] != text
        ):
            continue
        candidates: list[dict[str, Any]] = []
        raw_candidates = span.get("umls_candidates")
        if not isinstance(raw_candidates, list):
            raw_candidates = []
        for candidate in raw_candidates:
            if not isinstance(candidate, dict):
                continue
            cui = _compact_text(candidate.get("cui"))
            canonical_name = _compact_text(candidate.get("canonical_name"))
            score = candidate.get("linking_score")
            if (
                not cui
                or not canonical_name
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
                or not 0.0 <= score <= 1.0
            ):
                continue
            semantic_types = candidate.get("semantic_types")
            if not isinstance(semantic_types, list):
                semantic_types = []
            candidates.append(
                {
                    "cui": cui,
                    "canonical_name": canonical_name,
                    "semantic_types": [
                        value
                        for value in semantic_types
                        if isinstance(value, str)
                    ],
                    "linking_score": round(float(score), 6),
                }
            )
        key = (segment_id, start, end)
        if key in seen:
            continue
        seen.add(key)
        valid.append(
            {
                "segment_id": segment_id,
                "text": text,
                "start_char": start,
                "end_char": end,
                "umls_candidates": candidates,
                "linked": bool(candidates),
            }
        )
    return sorted(
        valid,
        key=lambda item: (
            item["segment_id"],
            item["start_char"],
            -(item["end_char"] - item["start_char"]),
        ),
    )

