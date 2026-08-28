from __future__ import annotations

import re
from typing import Any


_NUMBER_OR_UNIT_RE = re.compile(
    r"\d|영|일|이|삼|사|오|육|칠|팔|구|십|백|천|점|"
    r"mg|mcg|g|kg|ml|l|mmhg|bpm|%|도|밀리|그램",
    re.IGNORECASE,
)
_POLARITY_RE = re.compile(r"안|않|없|아니|못|모르|같|아마|추정|의심|\?")


def _semantic_markers(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return tuple(_NUMBER_OR_UNIT_RE.findall(text)), tuple(_POLARITY_RE.findall(text))


def apply_safe_corrections(
    raw_text: str,
    candidates: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    eligible_by_span: dict[tuple[int, int, str], list[dict[str, Any]]] = {}
    review_items: list[dict[str, Any]] = []

    for candidate in candidates:
        source_text = candidate.get("source_text")
        replacement = candidate.get("canonical_ko")
        start = candidate.get("start_char")
        end = candidate.get("end_char")
        approved_alias = (
            candidate.get("collection") != "kcd9_terms"
            and candidate.get("match_type") == "stt_alias_exact"
            and str(candidate.get("review_status") or "").casefold() == "approved"
        )
        if (
            not approved_alias
            or not isinstance(source_text, str)
            or not isinstance(replacement, str)
            or not replacement
            or source_text == replacement
        ):
            continue
        safe = (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start <= end <= len(raw_text)
            and raw_text[start:end] == source_text
            and _semantic_markers(source_text) == _semantic_markers(replacement)
        )
        if not safe:
            review_items.append(
                {
                    "type": "rejected_correction",
                    "source_text": source_text,
                    "replacement": replacement,
                    "reason": "automatic correction safety policy was not satisfied",
                }
            )
            continue
        eligible_by_span.setdefault((start, end, source_text), []).append(candidate)

    accepted: list[tuple[int, int, dict[str, Any]]] = []
    for (start, end, source_text), eligible in eligible_by_span.items():
        distinct = {
            (candidate.get("entity_id"), candidate.get("canonical_ko")): candidate
            for candidate in eligible
        }
        if len(distinct) != 1:
            review_items.append(
                {
                    "type": "ambiguous_approved_alias",
                    "source_text": source_text,
                    "candidate_count": len(distinct),
                    "reason": "multiple approved STT aliases matched the same span",
                }
            )
            continue
        accepted.append((start, end, next(iter(distinct.values()))))

    accepted.sort(key=lambda item: item[0])
    for left, right in zip(accepted, accepted[1:]):
        if left[1] > right[0]:
            return raw_text, [], review_items + [
                {
                    "type": "rejected_correction",
                    "reason": "overlapping corrections require review",
                }
            ]

    corrected = raw_text
    corrections: list[dict[str, Any]] = []
    for start, end, candidate in reversed(accepted):
        corrected = corrected[:start] + candidate["canonical_ko"] + corrected[end:]
    for start, end, candidate in accepted:
        corrections.append(
            {
                "source_span": {
                    "text": candidate["source_text"],
                    "start_char": start,
                    "end_char": end,
                },
                "replacement": candidate["canonical_ko"],
                "type": "stt_medical_term_error",
                "confidence": candidate.get("retrieval_score"),
                "needs_review": False,
                "candidate": {
                    key: value
                    for key, value in candidate.items()
                    if key not in {"source_text", "start_char", "end_char"}
                },
            }
        )
    return corrected, corrections, review_items

