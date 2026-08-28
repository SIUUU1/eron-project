from __future__ import annotations

import re


_QUESTION_RE = re.compile(r"[?？]")
_NEGATION_OR_UNCERTAINTY_RE = re.compile(
    r"안|않|없|아니|못|모르|같|아마|추정|의심"
)
_NUMERIC_VALUE_RE = re.compile(r"(?<![\d.])\d+(?:[.,]\d+)?(?![\d.])")
_UNIT_RE = re.compile(
    r"(?<=\d)\s*(mmHg|mcg|mg|kg|mL|ml|bpm|cm|mm|g|L|l|%|℃|도|점)",
    re.IGNORECASE,
)


def preservation_violations(raw_text: str, corrected_text: str) -> list[str]:
    """Return evidence categories changed by a proposed correction."""
    checks = (
        ("question", _QUESTION_RE),
        ("negation_or_uncertainty", _NEGATION_OR_UNCERTAINTY_RE),
        ("numeric_value", _NUMERIC_VALUE_RE),
        ("unit", _UNIT_RE),
    )
    return [
        name
        for name, pattern in checks
        if pattern.findall(raw_text) != pattern.findall(corrected_text)
    ]

