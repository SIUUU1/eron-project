from __future__ import annotations

import math
from typing import Any


def validate_whisper_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("input must be a JSON object")
    segments = payload.get("segments")
    if not isinstance(segments, list):
        raise ValueError("input must contain a segments array")

    seen_ids: set[str | int] = set()
    validated: list[dict[str, Any]] = []
    for position, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError(f"segment at position {position} must be an object")
        segment_id = segment.get("id")
        if not isinstance(segment_id, (str, int)) or isinstance(segment_id, bool):
            raise ValueError(f"segment at position {position} must contain an id")
        if segment_id in seen_ids:
            raise ValueError(f"duplicate segment id: {segment_id}")
        seen_ids.add(segment_id)

        text = segment.get("text")
        if not isinstance(text, str):
            raise ValueError(f"segment {segment_id} must contain text")
        start = segment.get("start")
        end = segment.get("end")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in (start, end)
        ):
            raise ValueError(f"segment {segment_id} must contain finite numeric start and end")
        if start > end:
            raise ValueError(f"segment {segment_id} start must not be after end")
        validated.append(segment)
    return validated

