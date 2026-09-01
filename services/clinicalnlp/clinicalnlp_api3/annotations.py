from __future__ import annotations

import re
from typing import Any


_TRUSTED_REVIEW_STATUSES = {
    "approved",
    "official",
    "official_reference",
    "source_imported",
    "manually_verified",
}
_MAX_CANDIDATES_PER_SPAN = 2
_MAX_MEDICAL_CANDIDATES_PER_DOCUMENT = 30
_GENERIC_MEDICAL_SPANS = {"환자", "양측"}
_SMOKING_HISTORY_CONTEXT_RE = re.compile(
    r"(?:\d+\s*)?팩\s*이어|pack\s*-?\s*year|흡연|금연",
    re.IGNORECASE,
)
_HYPERTENSION_STT_RE = re.compile(r"하이퍼\s*텐션", re.IGNORECASE)
_MATCH_TYPE_PRIORITY = {
    "stt_alias_exact": 5,
    "official_exact": 4,
    "alias_exact": 4,
    "contextual_phrase": 3,
    "contextual_alternative": 3,
    "fts_context": 2,
    "fts": 2,
    "vector_ngram": 1,
}

_SINO_DIGIT_VALUES = {
    "영": 0,
    "공": 0,
    "일": 1,
    "이": 2,
    "삼": 3,
    "사": 4,
    "오": 5,
    "육": 6,
    "륙": 6,
    "칠": 7,
    "팔": 8,
    "구": 9,
}
_NATIVE_ONE_VALUES = {
    "한": 1,
    "하나": 1,
    "두": 2,
    "둘": 2,
    "세": 3,
    "셋": 3,
    "네": 4,
    "넷": 4,
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
}
_NATIVE_TEN_VALUES = {
    "열": 10,
    "스물": 20,
    "스무": 20,
    "서른": 30,
    "마흔": 40,
    "쉰": 50,
    "예순": 60,
    "일흔": 70,
    "여든": 80,
    "아흔": 90,
}
_SINO_NUMBER_CHARACTERS = "영공일이삼사오육륙칠팔구십백천만억"
_SINO_INTEGER_PATTERN = (
    rf"(?:[{_SINO_NUMBER_CHARACTERS}]+"
    rf"(?:\s+[{_SINO_NUMBER_CHARACTERS}]+)*)"
)
_NATIVE_ONES_PATTERN = "|".join(
    sorted(_NATIVE_ONE_VALUES, key=len, reverse=True)
)
_NATIVE_TENS_PATTERN = "|".join(
    sorted(_NATIVE_TEN_VALUES, key=len, reverse=True)
)
_NATIVE_INTEGER_PATTERN = (
    rf"(?:(?:{_NATIVE_TENS_PATTERN})(?:\s*(?:{_NATIVE_ONES_PATTERN}))?"
    rf"|(?:{_NATIVE_ONES_PATTERN}))"
)
_KOREAN_INTEGER_PATTERN = (
    rf"(?:{_SINO_INTEGER_PATTERN}|{_NATIVE_INTEGER_PATTERN})"
)
_NUMBER_EXPRESSION_PATTERN = (
    rf"(?:\d+(?:\.\d+)?|{_KOREAN_INTEGER_PATTERN}"
    rf"(?:\s*점\s*(?:\d+|[영공일이삼사오육륙칠팔구]+))?)"
)
_SPOKEN_MEASUREMENT_UNIT_MAP = {
    "마이크로그램": "mcg",
    "밀리그램": "mg",
    "킬로그램": "kg",
    "그램": "g",
    "밀리리터": "mL",
    "리터": "L",
    "센티미터": "cm",
    "밀리미터": "mm",
    "퍼센트": "%",
    "프로": "%",
    "mcg": "mcg",
    "mg": "mg",
    "kg": "kg",
    "ml": "mL",
    "l": "L",
    "cm": "cm",
    "mm": "mm",
    "%": "%",
    "℃": "°C",
    "°c": "°C",
    "도": "°C",
}
_SPOKEN_MEASUREMENT_UNITS_PATTERN = "|".join(
    re.escape(unit)
    for unit in sorted(
        _SPOKEN_MEASUREMENT_UNIT_MAP, key=len, reverse=True
    )
)

_BLOOD_PRESSURE_RE = re.compile(r"(?<!\d)(\d{2,3})\s*(?:에|/)\s*(\d{2,3})(?!\d)")
_BLOOD_PRESSURE_CONTEXT_RE = re.compile(
    r"(?<![0-9A-Za-z가-힣])(?P<label>BP|혈압)(?:은|는|이|가)?\s*[:=]?\s*"
    rf"(?P<systolic>{_NUMBER_EXPRESSION_PATTERN})\s*(?:에|/|대)\s*"
    rf"(?P<diastolic>{_NUMBER_EXPRESSION_PATTERN})(?![0-9A-Za-z가-힣])",
    re.IGNORECASE,
)
_VITAL_SIGN_CONTEXTS = (
    (
        "heart_rate",
        re.compile(
            r"(?<![0-9A-Za-z가-힣])(?P<label>심박수|맥박|펄스|pulse|HR|PR)"
            r"(?:은|는|이|가)?\s*[:=]?\s*"
            rf"(?P<value>{_NUMBER_EXPRESSION_PATTERN})"
            r"(?:\s*(?P<raw_unit>bpm|회))?",
            re.IGNORECASE,
        ),
        "bpm",
    ),
    (
        "respiratory_rate",
        re.compile(
            r"(?<![0-9A-Za-z가-힣])(?P<label>호흡수|RR)"
            r"(?:은|는|이|가)?\s*[:=]?\s*"
            rf"(?P<value>{_NUMBER_EXPRESSION_PATTERN})"
            r"(?:\s*(?P<raw_unit>회))?",
            re.IGNORECASE,
        ),
        "breaths/min",
    ),
    (
        "oxygen_saturation",
        re.compile(
            r"(?<![0-9A-Za-z가-힣])"
            r"(?P<label>산소\s*포화도|SpO2|SPO(?:도|투)|saturation|세츄레이션|세추레이션|"
            r"세트리에이션|세트레이션|새츄레이션|새추레이션)"
            r"(?:은|는|이|가)?\s*[:=]?\s*(?:그래도\s+)?"
            rf"(?P<value>{_NUMBER_EXPRESSION_PATTERN})"
            r"(?:\s*(?P<raw_unit>%|퍼센트))?",
            re.IGNORECASE,
        ),
        "%",
    ),
    (
        "body_temperature",
        re.compile(
            r"(?<![0-9A-Za-z가-힣])(?P<label>체온|BT|temperature|템퍼러처)"
            r"(?:은|는|이|가)?\s*[:=]?\s*"
            rf"(?P<value>{_NUMBER_EXPRESSION_PATTERN})"
            r"(?:\s*(?P<raw_unit>℃|°C|도))?",
            re.IGNORECASE,
        ),
        "°C",
    ),
)
_MEASUREMENT_RE = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?)\s*(mmHg|mcg|mg|kg|mL|ml|bpm|cm|mm|g|L|l|%|℃|도|점)",
    re.IGNORECASE,
)
_SPOKEN_MEASUREMENT_RE = re.compile(
    rf"(?<![0-9A-Za-z가-힣])(?P<value>{_NUMBER_EXPRESSION_PATTERN})\s*"
    rf"(?P<raw_unit>{_SPOKEN_MEASUREMENT_UNITS_PATTERN})",
    re.IGNORECASE,
)
_UNIT_MAP = {
    "도": "°C",
    "℃": "°C",
    "ml": "mL",
    "l": "L",
    "mmhg": "mmHg",
    "bpm": "bpm",
    "mcg": "mcg",
    "mg": "mg",
    "kg": "kg",
    "g": "g",
    "cm": "cm",
    "mm": "mm",
    "%": "%",
    "점": "point",
}


def _parse_sino_integer(text: str) -> int | None:
    compact = text.replace(" ", "")
    if not compact:
        return None
    small_units = {"십": 10, "백": 100, "천": 1000}
    large_units = {"만": 10_000, "억": 100_000_000}
    total = 0
    section = 0
    current = 0
    for character in compact:
        if character in _SINO_DIGIT_VALUES:
            current = _SINO_DIGIT_VALUES[character]
        elif character in small_units:
            section += (current or 1) * small_units[character]
            current = 0
        elif character in large_units:
            section += current
            total += (section or 1) * large_units[character]
            section = 0
            current = 0
        else:
            return None
    return total + section + current


def _parse_native_integer(text: str) -> int | None:
    compact = text.replace(" ", "")
    if compact in _NATIVE_ONE_VALUES:
        return _NATIVE_ONE_VALUES[compact]
    for word, value in sorted(
        _NATIVE_TEN_VALUES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if not compact.startswith(word):
            continue
        remainder = compact[len(word):]
        if not remainder:
            return value
        if remainder in _NATIVE_ONE_VALUES:
            return value + _NATIVE_ONE_VALUES[remainder]
    return None


def _parse_number_expression(text: str) -> int | float | None:
    compact = text.replace(" ", "")
    if re.fullmatch(r"\d+", compact):
        return int(compact)
    if re.fullmatch(r"\d+\.\d+", compact):
        return float(compact)
    integer_text, separator, fraction_text = compact.partition("점")
    integer = _parse_native_integer(integer_text)
    if integer is None:
        integer = _parse_sino_integer(integer_text)
    if integer is None:
        return None
    if not separator:
        return integer
    if fraction_text.isdigit():
        fraction_digits = fraction_text
    else:
        try:
            fraction_digits = "".join(
                str(_SINO_DIGIT_VALUES[character]) for character in fraction_text
            )
        except KeyError:
            return None
    if not fraction_digits:
        return None
    return float(f"{integer}.{fraction_digits}")


def build_annotations(
    raw_text: str,
    candidates: list[dict[str, Any]],
    *,
    max_candidates_per_span: int = _MAX_CANDIDATES_PER_SPAN,
) -> list[dict[str, Any]]:
    groups: dict[
        tuple[str, int, int, str, str], dict[str, Any]
    ] = {}
    for candidate in candidates:
        source_text = candidate.get("source_text")
        start = candidate.get("start_char")
        end = candidate.get("end_char")
        if (
            not isinstance(source_text, str)
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end < start
            or raw_text[start:end] != source_text
        ):
            continue
        if source_text.strip() in _GENERIC_MEDICAL_SPANS:
            continue

        canonical_ko = str(candidate.get("canonical_ko") or "")
        canonical_en = str(candidate.get("canonical_en") or "").casefold()
        if (
            _SMOKING_HISTORY_CONTEXT_RE.search(raw_text)
            and (canonical_en == "immunity" or canonical_ko == "면역")
        ):
            continue
        if (
            _HYPERTENSION_STT_RE.search(source_text)
            and (
                canonical_en == "hypotension"
                or canonical_en == "orthostatic hypotension"
                or "저혈압" in canonical_ko
            )
        ):
            continue

        collection = candidate.get("collection")
        annotation_type = (
            "diagnosis_term_candidate"
            if collection == "kcd9_terms"
            else "medical_term_candidate"
        )
        candidate_output = {
            key: value
            for key, value in candidate.items()
            if key
            not in {
                "source_text",
                "start_char",
                "end_char",
                "_annotation_group_id",
                "_search_term_en",
                "_term_type",
            }
        }
        review_status = str(candidate.get("review_status") or "").casefold()
        annotation_group_id = str(candidate.get("_annotation_group_id") or "")
        key = (
            annotation_type,
            start,
            end,
            source_text,
            annotation_group_id,
        )
        group = groups.setdefault(
            key,
            {
                "type": annotation_type,
                "source_span": {
                    "text": source_text,
                    "start_char": start,
                    "end_char": end,
                },
                "candidates": [],
                "_review_flags": [],
                "_search_terms_en": [],
                "_term_types": [],
            },
        )
        group["candidates"].append(candidate_output)
        search_term_en = str(candidate.get("_search_term_en") or "").strip()
        if search_term_en and search_term_en not in group["_search_terms_en"]:
            group["_search_terms_en"].append(search_term_en)
        term_type = str(candidate.get("_term_type") or "").strip()
        if term_type and term_type not in group["_term_types"]:
            group["_term_types"].append(term_type)
        group["_review_flags"].append(
            collection == "kcd9_terms"
            or candidate.get("match_type")
            not in {"alias_exact", "official_exact", "stt_alias_exact"}
            or review_status not in _TRUSTED_REVIEW_STATUSES
        )

    annotations: list[dict[str, Any]] = []
    for group in groups.values():
        ranked = sorted(
            group["candidates"],
            key=lambda item: float(item.get("retrieval_score") or 0.0),
            reverse=True,
        )
        deduplicated: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        for candidate in ranked:
            identity = (
                candidate.get("collection"),
                candidate.get("entity_id"),
                candidate.get("canonical_ko"),
                candidate.get("canonical_en"),
                candidate.get("code"),
                candidate.get("code_display"),
            )
            if identity in seen:
                continue
            seen.add(identity)
            deduplicated.append(candidate)

        drug_type_candidates: dict[str, dict[str, Any]] = {}
        for candidate in deduplicated:
            entity_type = str(candidate.get("entity_type") or "").casefold()
            if (
                candidate.get("collection") == "drug_terms"
                and entity_type in {"ingredient", "product"}
            ):
                drug_type_candidates.setdefault(entity_type, candidate)

        if max_candidates_per_span >= 2 and len(drug_type_candidates) == 2:
            unique = sorted(
                drug_type_candidates.values(),
                key=lambda item: float(item.get("retrieval_score") or 0.0),
                reverse=True,
            )
            unique.extend(
                candidate
                for candidate in deduplicated
                if candidate not in unique
            )
            unique = unique[:max_candidates_per_span]
        else:
            unique = deduplicated[:max_candidates_per_span]
        annotation = {
            "type": group["type"],
            "source_span": group["source_span"],
            "candidates": unique,
            "needs_review": len(unique) > 1 or any(group["_review_flags"]),
        }
        if group["_search_terms_en"]:
            annotation["search_terms_en"] = list(group["_search_terms_en"])
        if len(group["_term_types"]) == 1:
            annotation["term_type"] = group["_term_types"][0]
        annotations.append(annotation)
    return annotations


def limit_document_medical_candidates(
    segments: list[dict[str, Any]],
    *,
    limit: int = _MAX_MEDICAL_CANDIDATES_PER_DOCUMENT,
) -> None:
    """Keep the strongest medical candidates without counting measurements."""
    ranked: list[tuple[int, float, int, int, int]] = []
    medical_types = {"medical_term_candidate", "diagnosis_term_candidate"}
    for segment_index, segment in enumerate(segments):
        for annotation_index, annotation in enumerate(segment["annotations"]):
            if annotation["type"] not in medical_types:
                continue
            for candidate_index, candidate in enumerate(annotation["candidates"]):
                ranked.append(
                    (
                        _MATCH_TYPE_PRIORITY.get(candidate.get("match_type"), 0),
                        float(candidate.get("retrieval_score") or 0.0),
                        segment_index,
                        annotation_index,
                        candidate_index,
                    )
                )

    strongest = sorted(
        ranked,
        key=lambda item: (-item[0], -item[1], item[2], item[3], item[4]),
    )[:limit]
    selected = {(item[2], item[3], item[4]) for item in strongest}

    for segment_index, segment in enumerate(segments):
        retained_annotations: list[dict[str, Any]] = []
        for annotation_index, annotation in enumerate(segment["annotations"]):
            if annotation["type"] not in medical_types:
                retained_annotations.append(annotation)
                continue
            annotation["candidates"] = [
                candidate
                for candidate_index, candidate in enumerate(annotation["candidates"])
                if (segment_index, annotation_index, candidate_index) in selected
            ]
            if annotation["candidates"]:
                retained_annotations.append(annotation)
        segment["annotations"] = retained_annotations


def detect_numeric_annotations(raw_text: str) -> list[dict[str, Any]]:
    annotations: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    for match in _BLOOD_PRESSURE_CONTEXT_RE.finditer(raw_text):
        systolic = _parse_number_expression(match.group("systolic"))
        diastolic = _parse_number_expression(match.group("diastolic"))
        if not isinstance(systolic, int) or not isinstance(diastolic, int):
            continue
        start, end = match.span("systolic")[0], match.span("diastolic")[1]
        occupied.append((start, end))
        annotations.append(
            {
                "type": "numeric_measurement_candidate",
                "source_span": {
                    "text": raw_text[start:end],
                    "start_char": start,
                    "end_char": end,
                },
                "candidates": [
                    {
                        "kind": "blood_pressure",
                        "systolic": systolic,
                        "diastolic": diastolic,
                        "unit": "mmHg",
                        "label": match.group("label"),
                    }
                ],
                "needs_review": False,
            }
        )

    for match in _BLOOD_PRESSURE_RE.finditer(raw_text):
        start, end = match.span()
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        occupied.append((start, end))
        annotations.append(
            {
                "type": "numeric_measurement_candidate",
                "source_span": {"text": match.group(0), "start_char": start, "end_char": end},
                "candidates": [
                    {
                        "kind": "blood_pressure",
                        "systolic": int(match.group(1)),
                        "diastolic": int(match.group(2)),
                        "unit": "mmHg",
                    }
                ],
                "needs_review": False,
            }
        )

    for kind, pattern, unit in _VITAL_SIGN_CONTEXTS:
        for match in pattern.finditer(raw_text):
            start, end = match.span("value")
            if match.group("raw_unit"):
                end = match.span("raw_unit")[1]
            if any(
                start < used_end and end > used_start
                for used_start, used_end in occupied
            ):
                continue
            value = _parse_number_expression(match.group("value"))
            if value is None:
                continue
            occupied.append((start, end))
            candidate = {
                "kind": kind,
                "value": value,
                "unit": unit,
                "label": match.group("label"),
            }
            if match.group("raw_unit"):
                candidate["raw_unit"] = match.group("raw_unit")
            annotations.append(
                {
                    "type": "numeric_measurement_candidate",
                    "source_span": {
                        "text": raw_text[start:end],
                        "start_char": start,
                        "end_char": end,
                    },
                    "candidates": [candidate],
                    "needs_review": False,
                }
            )

    for match in _SPOKEN_MEASUREMENT_RE.finditer(raw_text):
        start, end = match.span()
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        raw_value = match.group("value")
        raw_unit = match.group("raw_unit")
        separator = raw_text[match.span("value")[1]:match.span("raw_unit")[0]]
        if (
            any("가" <= character <= "힣" for character in raw_value)
            and any("가" <= character <= "힣" for character in raw_unit)
            and not separator
        ):
            continue
        value = _parse_number_expression(raw_value)
        if value is None:
            continue
        unit = _SPOKEN_MEASUREMENT_UNIT_MAP[raw_unit.casefold()]
        occupied.append((start, end))
        annotations.append(
            {
                "type": "numeric_measurement_candidate",
                "source_span": {
                    "text": raw_text[start:end],
                    "start_char": start,
                    "end_char": end,
                },
                "candidates": [
                    {
                        "kind": "measurement",
                        "value": value,
                        "unit": unit,
                        "raw_unit": raw_unit,
                    }
                ],
                "needs_review": False,
            }
        )

    for match in _MEASUREMENT_RE.finditer(raw_text):
        start, end = match.span()
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        raw_value, raw_unit = match.groups()
        value = float(raw_value) if "." in raw_value else int(raw_value)
        unit = _UNIT_MAP.get(raw_unit.casefold(), raw_unit)
        annotations.append(
            {
                "type": "numeric_measurement_candidate",
                "source_span": {"text": match.group(0), "start_char": start, "end_char": end},
                "candidates": [
                    {"kind": "measurement", "value": value, "unit": unit, "raw_unit": raw_unit}
                ],
                "needs_review": False,
            }
        )
    return sorted(annotations, key=lambda item: item["source_span"]["start_char"])

