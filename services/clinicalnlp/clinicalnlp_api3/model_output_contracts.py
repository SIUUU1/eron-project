from __future__ import annotations

from typing import Any, Iterable


MEDICAL_TERM_TYPES = (
    "allergy",
    "anatomy",
    "device",
    "disease_or_diagnosis",
    "drug",
    "symptom_or_sign",
    "test_procedure_or_surgery",
    "vital_or_numeric",
)


def _unique_segment_ids(segment_ids: Iterable[str]) -> list[str]:
    return list(
        dict.fromkeys(
            value for value in segment_ids if isinstance(value, str)
        )
    )


def _segment_id_schema(segment_ids: Iterable[str]) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string"}
    values = _unique_segment_ids(segment_ids)
    if values:
        schema["enum"] = values
    return schema


def _response_format(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": schema,
        },
    }


def translation_search_response_format(
    segment_ids: Iterable[str],
) -> dict[str, Any]:
    """Contract for full translation plus search-only medical terms."""

    return _response_format(
        "full_segment_medical_translation",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["segments"],
            "properties": {
                "segments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "segment_id",
                            "translated_text_en",
                            "medical_terms",
                        ],
                        "properties": {
                            "segment_id": _segment_id_schema(segment_ids),
                            "translated_text_en": {"type": "string"},
                            "medical_terms": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [
                                        "source_text",
                                        "search_terms_en",
                                        "term_type",
                                    ],
                                    "properties": {
                                        "source_text": {"type": "string"},
                                        "search_terms_en": {
                                            "type": "array",
                                            "minItems": 1,
                                            "maxItems": 1,
                                            "items": {"type": "string"},
                                        },
                                        "term_type": {
                                            "type": "string",
                                            "enum": list(MEDICAL_TERM_TYPES),
                                        },
                                    },
                                },
                            },
                        },
                    },
                }
            },
        },
    )


def compact_translation_response_format(
    translation_ids: Iterable[str],
) -> dict[str, Any]:
    """Bounded translation and medical-span contract keyed by transport IDs."""

    values = _unique_segment_ids(translation_ids)
    return _response_format(
        "compact_segment_translation",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["translations"],
            "properties": {
                "translations": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": values,
                    "properties": {
                        value: {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["translated_text_en", "medical_terms"],
                            "properties": {
                                "translated_text_en": {"type": "string"},
                                "medical_terms": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": [
                                            "source_text",
                                            "search_terms_en",
                                            "term_type",
                                        ],
                                        "properties": {
                                            "source_text": {"type": "string"},
                                            "search_terms_en": {
                                                "type": "array",
                                                "minItems": 1,
                                                "maxItems": 1,
                                                "items": {"type": "string"},
                                            },
                                            "term_type": {
                                                "type": "string",
                                                "enum": list(MEDICAL_TERM_TYPES),
                                            },
                                        },
                                    },
                                },
                            },
                        }
                        for value in values
                    },
                }
            },
        },
    )


def clinical_record_response_format(
    segment_ids: Iterable[str],
) -> dict[str, Any]:
    """Contract for clinical facts only; candidate decisions are excluded."""

    return _response_format(
        "clinical_record_extraction",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["clinical_record", "unresolved_questions"],
            "properties": {
                "clinical_record": {"type": "object"},
                "unresolved_questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["source_segment_id", "topic"],
                        "properties": {
                            "source_segment_id": _segment_id_schema(segment_ids),
                            "topic": {"type": "string"},
                        },
                    },
                },
            },
        },
    )


def candidate_adjudication_response_format() -> dict[str, Any]:
    """Fixed candidate-decision contract; workflow code grounds returned IDs."""

    return _response_format(
        "candidate_adjudication",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["candidate_decisions"],
            "properties": {
                "candidate_decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "segment_id",
                            "annotation_index",
                            "action",
                            "selected_candidate_ids",
                            "confidence",
                        ],
                        "properties": {
                            "segment_id": {"type": "string"},
                            "annotation_index": {
                                "type": "integer",
                                "minimum": 0,
                            },
                            "action": {
                                "type": "string",
                                "enum": [
                                    "selected",
                                    "rejected_all",
                                    "needs_review",
                                ],
                            },
                            "selected_candidate_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 2,
                                "uniqueItems": True,
                            },
                            "confidence": {
                                "type": ["number", "null"],
                                "minimum": 0,
                                "maximum": 1,
                            },
                        },
                    },
                }
            },
        },
    )


def draft_normalization_response_format(
    field_ids: Iterable[str],
    atom_ids: Iterable[str],
    candidate_ids: Iterable[str],
) -> dict[str, Any]:
    """Bounded contract for candidate-ID-only AI draft normalization."""

    fields = _unique_segment_ids(field_ids)
    atoms = _unique_segment_ids(atom_ids)
    candidates = _unique_segment_ids(candidate_ids)
    return _response_format(
        "clinical_draft_normalization",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["draft_suggestions"],
            "properties": {
                "draft_suggestions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "field_id",
                            "atom_id",
                            "suggested_value",
                            "applied_candidate_ids",
                        ],
                        "properties": {
                            "field_id": {"type": "string", "enum": fields},
                            "atom_id": {"type": "string", "enum": atoms},
                            "suggested_value": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 1024,
                            },
                            "applied_candidate_ids": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 3,
                                "uniqueItems": True,
                                "items": {
                                    "type": "string",
                                    "enum": candidates,
                                },
                            },
                        },
                    },
                }
            },
        },
    )

