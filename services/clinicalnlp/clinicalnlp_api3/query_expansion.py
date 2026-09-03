from __future__ import annotations

import json
import math
import os
import re
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .clinical_llm import ClinicalLlmClient, OllamaCloudClinicalLlmClient
from .model_output_contracts import (
    MEDICAL_TERM_TYPES,
    compact_translation_response_format,
    translation_search_response_format,
)


DEFAULT_MAX_OUTPUT_TOKENS = 3072
_TERM_TYPES = set(MEDICAL_TERM_TYPES)
_ENGLISH_QUERY_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9 .,+()/%'’:_-]{0,119}\Z"
)
_ENGLISH_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’/-]*")
_HANGUL_RE = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
_PROVIDER_COUNT_METRICS = (
    "provider_call_count",
    "network_retry_count",
    "rate_limit_count",
)
_PROVIDER_DURATION_METRICS = (
    "http_elapsed_ms",
    "provider_total_ms",
    "provider_load_ms",
    "provider_prompt_eval_ms",
    "provider_eval_ms",
)
_TRANSLATION_SEARCH_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "did", "do", "does", "for", "from", "had", "has", "have", "he",
    "her", "his", "i", "in", "is", "it", "its", "of", "on", "or",
    "she", "that", "the", "their", "they", "this", "to", "was", "were",
    "with", "you",
}
_GENERIC_COURSE_ONLY_RE = re.compile(
    r"^(?:더\s*)?(?:"
    r"있(?:는데|어요|습니다|다)?|없(?:는데|어요|습니다|다)?|"
    r"늘(?:었어요|었습니다|었다|어나요|어났어요)?|"
    r"증가(?:했어요|했습니다|했다|함)?|감소(?:했어요|했습니다|했다|함)?|"
    r"심해(?:졌어요|졌습니다|졌다|요)?|악화(?:됐어요|되었습니다|됨)?|"
    r"좋아(?:졌어요|졌습니다|졌다)?|호전(?:됐어요|되었습니다|됨)?|"
    r"increase|increased|worsening|improved|decreased"
    r")$",
    re.IGNORECASE,
)


def _accumulate_provider_diagnostics(
    target: dict[str, int | float],
    diagnostics: Any,
) -> None:
    if not isinstance(diagnostics, dict):
        return
    for key in _PROVIDER_COUNT_METRICS:
        value = diagnostics.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            target[key] = int(target.get(key, 0)) + value
    for key in _PROVIDER_DURATION_METRICS:
        value = diagnostics.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value >= 0
        ):
            target[key] = float(target.get(key, 0.0)) + float(value)

_SYSTEM_PROMPT = """You are a medical search-query preprocessor, not a clinical decision maker.
Read the whole supplied Korean+English mixed dialogue. Find only spans that may express a medical concept in these domains: symptom/sign, disease/diagnosis, drug, allergy, anatomy, test/procedure/surgery, vital/numeric, or device.
Favor recall for plausible medical expressions because every output is used for search and clinician review only. Scan every segment and emit every distinct medical expression, not just the most important expression in a segment. Include Korean lay symptom descriptions, mixed-language terms, abbreviations, and Korean phonetic renderings of English medical words.
Expressions joined by Korean or English conjunctions must be emitted as separate items. For example, if an input segment literally contains "열이 나고 가슴이 아파요", emit one exact span for the fever expression and another exact span for the chest-pain expression. The example explains the structure only; never emit its terms unless they occur in the supplied dialogue.

For every item:
- source_span.text must be an exact substring of the named segment, with exact zero-based Python character offsets.
- Preserve the source wording. Never rewrite, correct, delete, or replace it.
- Produce at most three concise English search terms. They are for local dictionary search only and are never facts or automatic chart values.
- Use dialogue context only to disambiguate a search query. Never invent a diagnosis or other absent fact.
- If a drug source span is an ingredient, do not convert it to a product; if it is a product, do not convert it to an ingredient.
- Do not emit translations for ordinary non-medical speech.
- When meaning is uncertain but the span is plausibly medical, provide a broad literal English search term without resolving it into a diagnosis.

Return only the requested JSON object."""

_FULL_TRANSLATION_SYSTEM_PROMPT = """You are a medical translation and dictionary-query preprocessor, not a clinical decision maker.
Translate every complete segment into natural English without omitting, adding, diagnosing, or resolving uncertain content. Keep each translation aligned to its segment_id.

For each segment, also return medical_terms containing only expressions useful for a medical dictionary search: symptom/sign, disease/diagnosis, drug, allergy, anatomy, test/procedure/surgery, vital/numeric, or device.
- source_text must be an exact substring copied from the original segment. Do not return character offsets.
- search_terms_en contains exactly one concise English medical dictionary term. Never copy the category list or these instructions into it.
- Do not put ordinary speech, discourse, temporal wording, or non-medical translation fragments in medical_terms.
- If a drug source is an ingredient, do not convert it to a product; if it is a product, do not convert it to an ingredient.
- The full translated_text_en is display-only. Only medical_terms.search_terms_en will be searched, and nothing is automatically written to the clinical record.

Return only the requested JSON object."""

_COMPACT_TRANSLATION_SYSTEM_PROMPT = """You are a clinical dialogue translator and medical search-query preprocessor, not a clinical decision maker.
Use every context segment to understand ambiguous Korean+English mixed medical speech. Translate only the requested target segments into natural English. Preserve uncertainty, negation, ingredient-versus-product naming level, and every clinical fact. Do not diagnose, summarize, add, or omit content. Keep uncertain phonetic terms literal when their meaning cannot be resolved safely.

For every target segment, also return each distinct medical expression in medical_terms. Expressions coordinated by commas, Korean conjunctions, or English conjunctions such as "and" or "or" must be separate items; never return only the most prominent expression.
- source_text must be an exact substring copied from that target's original text.
- search_terms_en contains exactly one concise English medical dictionary query.
- term_type must use the supplied schema category that best describes the literal expression.
- Include only symptoms/signs, explicit diseases/diagnoses, drugs, allergies, anatomy, tests/procedures/surgery, vitals/numeric clinical values, or devices.
- Do not infer diagnoses or add facts. Preserve ingredient-versus-product naming level.

Return only the requested compact translations JSON object. Do not return explanations or duplicate terms."""


def _masked_segments(
    compact_segments: list[dict[str, Any]], spans: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    masks_by_segment: dict[str, list[tuple[int, int]]] = {}
    for span in spans:
        segment_id = span.get("segment_id")
        start = span.get("start_char")
        end = span.get("end_char")
        if (
            isinstance(segment_id, str)
            and isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start < end
        ):
            masks_by_segment.setdefault(segment_id, []).append((start, end))
    masked: list[dict[str, Any]] = []
    for segment in compact_segments:
        characters = list(str(segment.get("text") or ""))
        for start, end in masks_by_segment.get(segment.get("id"), []):
            if end <= len(characters):
                characters[start:end] = [" "] * (end - start)
        masked.append({"id": segment.get("id"), "text": "".join(characters)})
    return masked


def _first_json_object(content: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    cleaned = content.replace("```json", "").replace("```JSON", "").replace(
        "```", ""
    )
    for position, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[position:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _translation_search_phrases(text: str, *, limit: int = 32) -> list[str]:
    """Create bounded English dictionary queries without changing source evidence."""

    tokens = [match.group(0) for match in _ENGLISH_TOKEN_RE.finditer(text)]
    phrases: list[str] = []
    seen: set[str] = set()
    for width in (4, 3, 2, 1):
        for position in range(0, len(tokens) - width + 1):
            window = tokens[position : position + width]
            folded = [token.casefold() for token in window]
            if (
                all(token in _TRANSLATION_SEARCH_STOPWORDS for token in folded)
                or folded[0] in _TRANSLATION_SEARCH_STOPWORDS
                or folded[-1] in _TRANSLATION_SEARCH_STOPWORDS
            ):
                continue
            phrase = " ".join(window)
            key = phrase.casefold()
            if key in seen:
                continue
            seen.add(key)
            phrases.append(phrase)
            if len(phrases) >= limit:
                return phrases
    return phrases


class LlamaServerMedicalQueryExpander:
    """Convert exact source spans into search-only English query variants."""

    def __init__(
        self,
        base_url: str,
        *,
        model_name: str = "gemma-4-E4B",
        context_size: int = 8192,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        max_passes: int = 1,
        timeout: float = 600,
        llm_client: ClinicalLlmClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.context_size = context_size
        self.max_output_tokens = max_output_tokens
        if not 1 <= max_passes <= 3:
            raise ValueError("max_passes must be between 1 and 3")
        self.max_passes = max_passes
        self.timeout = timeout
        self.llm_client = llm_client

    @classmethod
    def from_environment(cls) -> "LlamaServerMedicalQueryExpander":
        provider = os.environ.get("CLINICAL_LLM_PROVIDER", "local").strip().casefold()
        if provider not in {"local", "ollama_cloud"}:
            raise ValueError(f"Unsupported CLINICAL_LLM_PROVIDER: {provider}")
        cloud = provider == "ollama_cloud"
        base_url = (
            os.environ.get("OLLAMA_BASE_URL", "https://ollama.com")
            if cloud
            else os.environ.get(
                "CLINICALNLP_API3_GEMMA_URL",
                os.environ.get("CLINICALNLP_API2_GEMMA_URL", "http://127.0.0.1:8081"),
            )
        )
        model_name = (
            os.environ.get("OLLAMA_MODEL", "gemma4:31b")
            if cloud
            else os.environ.get(
                "CLINICALNLP_API3_GEMMA_MODEL",
                os.environ.get("CLINICALNLP_API2_GEMMA_MODEL", "gemma-4-E4B"),
            )
        )
        max_output_tokens = int(
            os.environ.get(
                "CLINICALNLP_QUERY_EXPANSION_MAX_TOKENS",
                str(DEFAULT_MAX_OUTPUT_TOKENS),
            )
        )
        timeout = float(
            os.environ.get(
                "OLLAMA_TIMEOUT" if cloud else "CLINICALNLP_API3_GEMMA_TIMEOUT",
                "600",
            )
        )
        llm_client: ClinicalLlmClient | None = None
        if cloud:
            llm_client = OllamaCloudClinicalLlmClient(
                base_url,
                model_name=model_name,
                api_key=os.environ.get("OLLAMA_API_KEY", ""),
                max_output_tokens=max_output_tokens,
                timeout=timeout,
            )
        return cls(
            base_url,
            model_name=model_name,
            context_size=int(os.environ.get("CLINICALNLP_API3_CONTEXT", "8192")),
            max_output_tokens=max_output_tokens,
            max_passes=int(os.environ.get("CLINICALNLP_QUERY_EXPANSION_PASSES", "1")),
            timeout=timeout,
            llm_client=llm_client,
        )

    def _request_compact_translation(
        self,
        *,
        context_segments: list[dict[str, str]],
        target_segment_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        user_payload = {
            "context_segments": context_segments,
            "target_segment_ids": target_segment_ids,
        }
        response_format = compact_translation_response_format(target_segment_ids)
        if self.llm_client is not None:
            model_result = self.llm_client.generate_json(
                system_prompt=_COMPACT_TRANSLATION_SYSTEM_PROMPT,
                user_payload=user_payload,
                response_format=response_format,
                output_label="compact translation",
            )
            translations = model_result.get("translations")
            if not isinstance(translations, dict):
                raise ValueError("InvalidModelResponse")
            normalized: dict[str, dict[str, Any]] = {}
            for target_id in target_segment_ids:
                translated = translations.get(target_id)
                # Accept the former string-only payload as a bounded fallback
                # while deployed model servers roll onto the richer schema.
                if isinstance(translated, str):
                    translated = {
                        "translated_text_en": translated,
                        "medical_terms": [],
                    }
                if not isinstance(translated, dict):
                    raise ValueError("InvalidModelResponse")
                translated_text = translated.get("translated_text_en")
                medical_terms = translated.get("medical_terms")
                if not isinstance(translated_text, str) or not translated_text.strip():
                    raise ValueError("InvalidModelResponse")
                if not isinstance(medical_terms, list):
                    raise ValueError("InvalidModelResponse")
                normalized[target_id] = {
                    "translated_text_en": " ".join(translated_text.split()),
                    "medical_terms": medical_terms,
                }
            return normalized
        request_payload = json.dumps(
            {
                "model": self.model_name,
                "messages": [
                    {"role": "system", "content": _COMPACT_TRANSLATION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                **user_payload,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                "temperature": 0,
                "max_tokens": min(
                    self.max_output_tokens,
                    max(512, self.context_size // 2),
                ),
                "response_format": response_format,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/v1/chat/completions",
            data=request_payload,
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        choice = result["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("OutputLengthExceeded")
        content = choice["message"]["content"]
        model_result = _first_json_object(content) if isinstance(content, str) else None
        translations = (
            model_result.get("translations")
            if isinstance(model_result, dict)
            else None
        )
        if not isinstance(translations, dict):
            raise ValueError("InvalidModelResponse")
        normalized: dict[str, dict[str, Any]] = {}
        for target_id in target_segment_ids:
            translated = translations.get(target_id)
            if isinstance(translated, str):
                translated = {
                    "translated_text_en": translated,
                    "medical_terms": [],
                }
            if not isinstance(translated, dict):
                raise ValueError("InvalidModelResponse")
            translated_text = translated.get("translated_text_en")
            medical_terms = translated.get("medical_terms")
            if not isinstance(translated_text, str) or not translated_text.strip():
                raise ValueError("InvalidModelResponse")
            if not isinstance(medical_terms, list):
                raise ValueError("InvalidModelResponse")
            normalized[target_id] = {
                "translated_text_en": " ".join(translated_text.split()),
                "medical_terms": medical_terms,
            }
        return normalized

    def _request_translation_batch_with_retry(
        self,
        *,
        context_segments: list[dict[str, str]],
        target_segment_ids: list[str],
        call_counter: list[int] | None = None,
        provider_telemetry: dict[str, int | float] | None = None,
        retry_telemetry: dict[str, int] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], list[tuple[str, Exception]]]:
        def request(ids: list[str]) -> dict[str, dict[str, Any]]:
            if call_counter is not None:
                call_counter[0] += 1
            try:
                return self._request_compact_translation(
                    context_segments=context_segments,
                    target_segment_ids=ids,
                )
            finally:
                diagnostics_reader = getattr(
                    self.llm_client,
                    "last_diagnostics",
                    None,
                )
                if provider_telemetry is not None and callable(
                    diagnostics_reader
                ):
                    _accumulate_provider_diagnostics(
                        provider_telemetry,
                        diagnostics_reader(),
                    )

        try:
            return (
                request(target_segment_ids),
                [],
            )
        except Exception as error:
            diagnostics_reader = getattr(
                self.llm_client,
                "last_diagnostics",
                None,
            )
            if (
                retry_telemetry is not None
                and isinstance(error, HTTPError)
                and error.code == 429
                and not callable(diagnostics_reader)
            ):
                retry_telemetry["direct_rate_limit_count"] = (
                    retry_telemetry.get("direct_rate_limit_count", 0) + 1
                )
            if isinstance(error, HTTPError) and error.code == 429:
                raise
            if len(target_segment_ids) == 1:
                return {}, [(target_segment_ids[0], error)]
        if retry_telemetry is not None:
            retry_telemetry["split_count"] = (
                retry_telemetry.get("split_count", 0) + 1
            )
        midpoint = len(target_segment_ids) // 2
        translations: dict[str, dict[str, Any]] = {}
        failures: list[tuple[str, Exception]] = []
        for target_ids in (
            target_segment_ids[:midpoint],
            target_segment_ids[midpoint:],
        ):
            partial_translations, partial_failures = (
                self._request_translation_batch_with_retry(
                    context_segments=context_segments,
                    target_segment_ids=target_ids,
                    call_counter=call_counter,
                    provider_telemetry=provider_telemetry,
                    retry_telemetry=retry_telemetry,
                )
            )
            translations.update(partial_translations)
            failures.extend(partial_failures)
        return translations, failures

    @staticmethod
    def _estimated_tokens(value: Any) -> int:
        text = (
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        )
        # UTF-8 bytes / 3 intentionally overestimates ordinary English while
        # remaining conservative for Korean and mixed clinical dialogue.
        return max(1, math.ceil(len(text.encode("utf-8")) / 3))

    def _translation_request_fits(
        self,
        *,
        context_segments: list[dict[str, str]],
        target_segment_ids: list[str],
    ) -> bool:
        user_payload = {
            "context_segments": context_segments,
            "target_segment_ids": target_segment_ids,
        }
        response_format = compact_translation_response_format(target_segment_ids)
        input_tokens = (
            self._estimated_tokens(_COMPACT_TRANSLATION_SYSTEM_PROMPT)
            + self._estimated_tokens(user_payload)
            + self._estimated_tokens(response_format)
            + 64
        )
        target_ids = set(target_segment_ids)
        source_tokens = sum(
            self._estimated_tokens(segment["text"])
            for segment in context_segments
            if segment["segment_id"] in target_ids
        )
        estimated_output_tokens = max(
            96,
            math.ceil(source_tokens * 2.25) + 40 * len(target_segment_ids),
        )
        context_budget = max(256, math.floor(self.context_size * 0.9))
        output_budget = max(128, math.floor(self.max_output_tokens * 0.9))
        return (
            estimated_output_tokens <= output_budget
            and input_tokens + estimated_output_tokens <= context_budget
        )

    def _translation_context_for_range(
        self,
        segments: list[dict[str, str]],
        start: int,
        end: int,
    ) -> list[dict[str, str]]:
        target_ids = [segment["segment_id"] for segment in segments[start:end]]
        if self._translation_request_fits(
            context_segments=segments,
            target_segment_ids=target_ids,
        ):
            return segments

        left = max(0, start - 2)
        right = min(len(segments), end + 2)
        context_segments = segments[left:right]
        while not self._translation_request_fits(
            context_segments=context_segments,
            target_segment_ids=target_ids,
        ) and (left < start or right > end):
            left_distance = start - left
            right_distance = right - end
            if right > end and right_distance >= left_distance:
                right -= 1
            elif left < start:
                left += 1
            context_segments = segments[left:right]
        return context_segments

    def _translation_batches(
        self,
        segments: list[dict[str, str]],
    ) -> list[tuple[list[dict[str, str]], list[str]]]:
        batches: list[tuple[list[dict[str, str]], list[str]]] = []

        def add_range(start: int, end: int) -> None:
            target_ids = [
                segment["segment_id"] for segment in segments[start:end]
            ]
            context_segments = self._translation_context_for_range(
                segments,
                start,
                end,
            )
            if end - start == 1 or self._translation_request_fits(
                context_segments=context_segments,
                target_segment_ids=target_ids,
            ):
                batches.append((context_segments, target_ids))
                return
            midpoint = start + (end - start) // 2
            add_range(start, midpoint)
            add_range(midpoint, end)

        if segments:
            add_range(0, len(segments))
        return batches

    @staticmethod
    def _response_format(segment_ids: list[str]) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "medical_query_expansion",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["items"],
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "segment_id",
                                    "source_span",
                                    "search_terms_en",
                                    "term_type",
                                ],
                                "properties": {
                                    "segment_id": {
                                        "type": "string",
                                        "enum": segment_ids,
                                    },
                                    "source_span": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": [
                                            "text",
                                            "start_char",
                                            "end_char",
                                        ],
                                        "properties": {
                                            "text": {"type": "string"},
                                            "start_char": {
                                                "type": "integer",
                                                "minimum": 0,
                                            },
                                            "end_char": {
                                                "type": "integer",
                                                "minimum": 1,
                                            },
                                        },
                                    },
                                    "search_terms_en": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 3,
                                        "items": {"type": "string"},
                                    },
                                    "term_type": {
                                        "type": "string",
                                        "enum": sorted(_TERM_TYPES),
                                    },
                                },
                            },
                        }
                    },
                },
            },
        }

    @staticmethod
    def _full_translation_response_format(segment_ids: list[str]) -> dict[str, Any]:
        return translation_search_response_format(segment_ids)

    def _request_full_translation(
        self, compact_segments: list[dict[str, Any]]
    ) -> dict[str, Any]:
        segment_ids = [
            segment["id"]
            for segment in compact_segments
            if isinstance(segment.get("id"), str)
        ]
        user_payload = {"segments": compact_segments}
        response_format = self._full_translation_response_format(segment_ids)
        if self.llm_client is not None:
            model_result = self.llm_client.generate_json(
                system_prompt=_FULL_TRANSLATION_SYSTEM_PROMPT,
                user_payload=user_payload,
                response_format=response_format,
                output_label="full segment translation",
            )
            if not isinstance(model_result.get("segments"), list):
                raise ValueError("InvalidModelResponse")
            return model_result
        request_payload = json.dumps(
            {
                "model": self.model_name,
                "messages": [
                    {"role": "system", "content": _FULL_TRANSLATION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            user_payload, ensure_ascii=False
                        ),
                    },
                ],
                "temperature": 0,
                "max_tokens": min(
                    self.max_output_tokens,
                    max(512, self.context_size // 2),
                ),
                "response_format": response_format,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/v1/chat/completions",
            data=request_payload,
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        content = result["choices"][0]["message"]["content"]
        model_result = _first_json_object(content) if isinstance(content, str) else None
        if not isinstance(model_result, dict) or not isinstance(
            model_result.get("segments"), list
        ):
            raise ValueError("InvalidModelResponse")
        return model_result

    @staticmethod
    def _sanitize_full_translation(
        model_result: dict[str, Any], compact_segments: list[dict[str, Any]]
    ) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
        text_by_id = {
            segment.get("id"): segment.get("text")
            for segment in compact_segments
            if isinstance(segment.get("id"), str)
            and isinstance(segment.get("text"), str)
        }
        translated_segments: list[dict[str, str]] = []
        items: list[dict[str, Any]] = []
        seen_segments: set[str] = set()
        seen_items: set[tuple[str, int, int, str]] = set()
        for translated in model_result.get("segments", []):
            if not isinstance(translated, dict):
                continue
            segment_id = translated.get("segment_id")
            translated_text = translated.get("translated_text_en")
            source_segment = text_by_id.get(segment_id)
            if (
                not isinstance(segment_id, str)
                or not isinstance(source_segment, str)
                or segment_id in seen_segments
                or not isinstance(translated_text, str)
                or not translated_text.strip()
                or _HANGUL_RE.search(translated_text)
            ):
                continue
            seen_segments.add(segment_id)
            translated_segments.append(
                {
                    "segment_id": segment_id,
                    "translated_text_en": " ".join(translated_text.split()),
                }
            )
            for term in translated.get("medical_terms", []):
                if not isinstance(term, dict) or term.get("term_type") not in _TERM_TYPES:
                    continue
                source_text = term.get("source_text")
                if not isinstance(source_text, str) or not source_text:
                    continue
                start = source_segment.find(source_text)
                if start < 0:
                    continue
                end = start + len(source_text)
                queries: list[str] = []
                seen_queries: set[str] = set()
                for value in term.get("search_terms_en", []):
                    query = " ".join(value.split()) if isinstance(value, str) else ""
                    folded = query.casefold()
                    if (
                        not query
                        or folded in seen_queries
                        or _HANGUL_RE.search(query)
                        or not _ENGLISH_QUERY_RE.fullmatch(query)
                    ):
                        continue
                    seen_queries.add(folded)
                    queries.append(query)
                    if len(queries) == 1:
                        break
                if not queries:
                    continue
                key = (segment_id, start, end, str(term["term_type"]))
                if key in seen_items:
                    continue
                seen_items.add(key)
                items.append(
                    {
                        "segment_id": segment_id,
                        "source_span": {
                            "text": source_text,
                            "start_char": start,
                            "end_char": end,
                        },
                        "search_terms_en": queries,
                        "term_type": term["term_type"],
                        "expansion_method": "GEMMA_FULL_SEGMENT_TRANSLATION",
                    }
                )
        order = {
            segment["id"]: index
            for index, segment in enumerate(compact_segments)
            if isinstance(segment.get("id"), str)
        }
        translated_segments.sort(key=lambda item: order.get(item["segment_id"], 10**9))
        items.sort(
            key=lambda item: (
                order.get(item["segment_id"], 10**9),
                item["source_span"]["start_char"],
                item["source_span"]["end_char"],
            )
        )
        return translated_segments, items

    @staticmethod
    def _sanitize_items(
        model_items: list[Any], segments: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        text_by_id = {
            segment.get("id"): segment.get("text")
            for segment in segments
            if isinstance(segment.get("id"), str)
            and isinstance(segment.get("text"), str)
        }
        merged: dict[tuple[str, int, int, str], dict[str, Any]] = {}
        for item in model_items:
            if not isinstance(item, dict):
                continue
            segment_id = item.get("segment_id")
            source_span = item.get("source_span")
            term_type = item.get("term_type")
            if (
                segment_id not in text_by_id
                or not isinstance(source_span, dict)
                or term_type not in _TERM_TYPES
            ):
                continue
            source_text = source_span.get("text")
            start = source_span.get("start_char")
            end = source_span.get("end_char")
            segment_text = text_by_id[segment_id]
            if (
                not isinstance(source_text, str)
                or not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or start < 0
                or end <= start
                or end > len(segment_text)
                or segment_text[start:end] != source_text
            ):
                continue
            queries: list[str] = []
            seen_queries: set[str] = set()
            for value in item.get("search_terms_en", []):
                query = " ".join(value.split()) if isinstance(value, str) else ""
                folded = query.casefold()
                if (
                    not query
                    or folded in seen_queries
                    or _HANGUL_RE.search(query)
                    or not _ENGLISH_QUERY_RE.fullmatch(query)
                ):
                    continue
                seen_queries.add(folded)
                queries.append(query)
                if len(queries) == 3:
                    break
            if not queries:
                continue
            key = (segment_id, start, end, term_type)
            if key not in merged:
                expansion_method = item.get("expansion_method")
                if expansion_method not in {
                    "GEMMA_CONTEXTUAL",
                    "GEMMA_SEGMENT_TRANSLATION",
                }:
                    expansion_method = "GEMMA_CONTEXTUAL"
                merged[key] = {
                    "segment_id": segment_id,
                    "source_span": {
                        "text": source_text,
                        "start_char": start,
                        "end_char": end,
                    },
                    "search_terms_en": [],
                    "term_type": term_type,
                    "expansion_method": expansion_method,
                }
            combined = merged[key]["search_terms_en"]
            existing = {query.casefold() for query in combined}
            combined.extend(
                query for query in queries if query.casefold() not in existing
            )
            del combined[3:]
        return sorted(
            merged.values(),
            key=lambda item: (
                item["segment_id"],
                item["source_span"]["start_char"],
                item["source_span"]["end_char"],
            ),
        )

    def _request_items(
        self,
        *,
        compact_segments: list[dict[str, Any]],
        segment_ids: list[str],
        already_found: list[dict[str, Any]],
    ) -> list[Any]:
        audit_prompt = ""
        if already_found:
            audit_prompt = (
                "\n\nThis is a completeness audit of the same whole dialogue. "
                "The user payload lists already_found_spans. Do not repeat those "
                "exact spans. Return only additional distinct medical expressions "
                "missed by the previous scan. segments contains the same text "
                "with already-found spans replaced by equal-length spaces. Focus on "
                "the visible text in segments, but copy source text and offsets "
                "from original_segments. Inspect every remaining phrase."
            )
        user_payload: dict[str, Any] = {"segments": compact_segments}
        if already_found:
            user_payload["already_found_spans"] = already_found
            audit_segments = _masked_segments(compact_segments, already_found)
            user_payload["segments"] = audit_segments
            user_payload["original_segments"] = compact_segments
            user_payload["audit_segments"] = audit_segments
        system_prompt = _SYSTEM_PROMPT + audit_prompt
        response_format = self._response_format(segment_ids)
        if self.llm_client is not None:
            model_result = self.llm_client.generate_json(
                system_prompt=system_prompt,
                user_payload=user_payload,
                response_format=response_format,
                output_label="medical query expansion",
            )
            if not isinstance(model_result.get("items"), list):
                raise ValueError("InvalidModelResponse")
            return model_result["items"]
        request_payload = json.dumps(
            {
                "model": self.model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(user_payload, ensure_ascii=False),
                    },
                ],
                "temperature": 0,
                "max_tokens": min(
                    self.max_output_tokens,
                    max(512, self.context_size // 4),
                ),
                "response_format": response_format,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            f"{self.base_url}/v1/chat/completions",
            data=request_payload,
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        content = result["choices"][0]["message"]["content"]
        model_result = _first_json_object(content) if isinstance(content, str) else None
        if not isinstance(model_result, dict) or not isinstance(
            model_result.get("items"), list
        ):
            raise ValueError("InvalidModelResponse")
        return model_result["items"]

    @staticmethod
    def _coarse_response_format(segment_ids: list[str]) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "segment_query_fallback",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["items"],
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "segment_id",
                                    "source_text",
                                    "search_terms_en",
                                    "term_type",
                                ],
                                "properties": {
                                    "segment_id": {
                                        "type": "string",
                                        "enum": segment_ids,
                                    },
                                    "source_text": {"type": "string"},
                                    "search_terms_en": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 3,
                                        "items": {"type": "string"},
                                    },
                                    "term_type": {
                                        "type": "string",
                                        "enum": sorted(_TERM_TYPES),
                                    },
                                },
                            },
                        }
                    },
                },
            },
        }

    def _request_segment_translation(
        self,
        *,
        compact_segments: list[dict[str, Any]],
        segment_ids: list[str],
        covered_spans: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        focus_segments = _masked_segments(compact_segments, covered_spans)
        task = (
            "For each supplied Korean or English segment, copy the exact visible "
            "substring that expresses a medical symptom or concept and translate "
            "it into concise English dictionary search terms. Ignore blank-masked "
            "text. Do not diagnose or add facts. source_text must be an exact visible "
            "substring. Return JSON only."
        )
        user_payload = {"segments": focus_segments}
        response_format = self._coarse_response_format(segment_ids)
        if self.llm_client is not None:
            model_result = self.llm_client.generate_json(
                system_prompt=task,
                user_payload=user_payload,
                response_format=response_format,
                output_label="segment query fallback",
            )
            if not isinstance(model_result.get("items"), list):
                raise ValueError("InvalidModelResponse")
        else:
            model_result = None
        if model_result is None:
            request_payload = json.dumps(
                {
                    "model": self.model_name,
                    "messages": [
                        {"role": "system", "content": task},
                        {
                            "role": "user",
                            "content": json.dumps(
                                user_payload, ensure_ascii=False
                            ),
                        },
                    ],
                    "temperature": 0,
                    "max_tokens": min(768, self.max_output_tokens),
                    "response_format": response_format,
                    "stream": False,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            request = Request(
                f"{self.base_url}/v1/chat/completions",
                data=request_payload,
                headers={"content-type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            content = result["choices"][0]["message"]["content"]
            model_result = (
                _first_json_object(content) if isinstance(content, str) else None
            )
            if not isinstance(model_result, dict) or not isinstance(
                model_result.get("items"), list
            ):
                raise ValueError("InvalidModelResponse")
        focus_by_id = {segment["id"]: segment["text"] for segment in focus_segments}
        translated: list[dict[str, Any]] = []
        for item in model_result["items"]:
            if not isinstance(item, dict):
                continue
            segment_id = item.get("segment_id")
            source_text = item.get("source_text")
            focus_text = focus_by_id.get(segment_id)
            if not isinstance(source_text, str) or not isinstance(focus_text, str):
                continue
            if _GENERIC_COURSE_ONLY_RE.fullmatch(
                source_text.strip().strip(".,!?;:")
            ):
                continue
            start = focus_text.find(source_text)
            if start < 0:
                continue
            translated.append(
                {
                    "segment_id": segment_id,
                    "source_span": {
                        "text": source_text,
                        "start_char": start,
                        "end_char": start + len(source_text),
                    },
                    "search_terms_en": item.get("search_terms_en"),
                    "term_type": item.get("term_type"),
                    "expansion_method": "GEMMA_SEGMENT_TRANSLATION",
                }
            )
        return translated

    @staticmethod
    def _error_code(error: Exception) -> str:
        if isinstance(error, ValueError) and str(error) in {
            "InvalidModelResponse",
            "OutputLengthExceeded",
        }:
            return str(error)
        return type(error).__name__

    def expand(
        self,
        segments: list[dict[str, Any]],
        *,
        covered_spans: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        compact_segments = [
            {
                "id": segment.get("id"),
                "start": segment.get("start"),
                "end": segment.get("end"),
                "text": segment.get("text"),
            }
            for segment in segments
        ]
        transport_segments = [
            {
                "segment_id": f"t{index:04d}",
                "text": str(segment.get("text") or ""),
            }
            for index, segment in enumerate(compact_segments, start=1)
        ]
        original_ids = {
            transport["segment_id"]: compact["id"]
            for transport, compact in zip(transport_segments, compact_segments)
        }
        translation_started = time.perf_counter()
        translation_calls = [0]
        provider_telemetry: dict[str, int | float] = {}
        retry_telemetry = {
            "split_count": 0,
            "direct_rate_limit_count": 0,
        }
        translation_batches: list[dict[str, int | float]] = []
        planned_batch_count = 0

        def translation_telemetry() -> dict[str, Any]:
            http_ms = float(provider_telemetry.get("http_elapsed_ms", 0.0))
            provider_ms = float(
                provider_telemetry.get("provider_total_ms", 0.0)
            )
            rate_limit_count = int(
                provider_telemetry.get("rate_limit_count", 0)
            ) + retry_telemetry["direct_rate_limit_count"]
            return {
                "translation_ms": round(
                    (time.perf_counter() - translation_started) * 1000,
                    3,
                ),
                "translation_calls": translation_calls[0],
                "translation_batch_count": planned_batch_count,
                "translation_retry_split_count": retry_telemetry["split_count"],
                "translation_rate_limit_count": rate_limit_count,
                "translation_batches": [
                    dict(batch) for batch in translation_batches
                ],
                "translation_provider_calls": int(
                    provider_telemetry.get("provider_call_count", 0)
                ),
                "translation_network_retries": int(
                    provider_telemetry.get("network_retry_count", 0)
                ),
                "translation_http_ms": round(http_ms, 3),
                "translation_provider_ms": round(provider_ms, 3),
                "translation_provider_load_ms": round(
                    float(provider_telemetry.get("provider_load_ms", 0.0)),
                    3,
                ),
                "translation_prompt_eval_ms": round(
                    float(
                        provider_telemetry.get("provider_prompt_eval_ms", 0.0)
                    ),
                    3,
                ),
                "translation_token_eval_ms": round(
                    float(provider_telemetry.get("provider_eval_ms", 0.0)),
                    3,
                ),
                "translation_unattributed_http_ms": round(
                    max(0.0, http_ms - provider_ms),
                    3,
                ),
            }

        try:
            translated_payloads: dict[str, dict[str, Any]] = {}
            failed_translations: list[tuple[str, Exception]] = []
            planned_batches = self._translation_batches(transport_segments)
            planned_batch_count = len(planned_batches)
            for batch_index, (context_segments, target_ids) in enumerate(
                planned_batches
            ):
                batch_started = time.perf_counter()
                calls_before = translation_calls[0]
                splits_before = retry_telemetry["split_count"]
                rate_limits_before = int(
                    provider_telemetry.get("rate_limit_count", 0)
                ) + retry_telemetry["direct_rate_limit_count"]
                rate_limit_error: HTTPError | None = None
                try:
                    translations, failures = (
                        self._request_translation_batch_with_retry(
                            context_segments=context_segments,
                            target_segment_ids=target_ids,
                            call_counter=translation_calls,
                            provider_telemetry=provider_telemetry,
                            retry_telemetry=retry_telemetry,
                        )
                    )
                except HTTPError as error:
                    if error.code != 429:
                        raise
                    rate_limit_error = error
                    translations = {}
                    failures = [
                        (target_id, error) for target_id in target_ids
                    ]
                rate_limits_after = int(
                    provider_telemetry.get("rate_limit_count", 0)
                ) + retry_telemetry["direct_rate_limit_count"]
                translation_batches.append(
                    {
                        "batch_index": batch_index,
                        "target_segment_count": len(target_ids),
                        "context_segment_count": len(context_segments),
                        "request_count": translation_calls[0] - calls_before,
                        "retry_split_count": (
                            retry_telemetry["split_count"] - splits_before
                        ),
                        "rate_limit_count": (
                            rate_limits_after - rate_limits_before
                        ),
                        "failed_segment_count": len(failures),
                        "elapsed_ms": round(
                            (time.perf_counter() - batch_started) * 1000,
                            3,
                        ),
                    }
                )
                failed_translations.extend(failures)
                translated_payloads.update(translations)
                if rate_limit_error is not None:
                    for _, remaining_ids in planned_batches[batch_index + 1 :]:
                        failed_translations.extend(
                            (target_id, rate_limit_error)
                            for target_id in remaining_ids
                        )
                    break
        except Exception as error:
            return {
                "status": "unavailable",
                "fallback_used": True,
                "method": "GEMMA_FULL_SEGMENT_TRANSLATION",
                "translated_segments": [],
                "items": [],
                "error_code": self._error_code(error),
                "_telemetry": translation_telemetry(),
            }
        if not translated_payloads and failed_translations:
            return {
                "status": "unavailable",
                "fallback_used": True,
                "method": "GEMMA_FULL_SEGMENT_TRANSLATION",
                "translated_segments": [],
                "items": [],
                "failed_segment_ids": [
                    original_ids[target_id]
                    for target_id, _ in failed_translations
                ],
                "error_code": self._error_code(failed_translations[0][1]),
                "_telemetry": translation_telemetry(),
            }
        translated_segments, items = self._sanitize_full_translation(
            {
                "segments": [
                    {
                        "segment_id": original_ids[target_id],
                        **translated,
                    }
                    for target_id, translated in translated_payloads.items()
                    if target_id in original_ids
                ]
            },
            compact_segments,
        )
        if not translated_segments and translated_payloads:
            return {
                "status": "unavailable",
                "fallback_used": True,
                "method": "GEMMA_FULL_SEGMENT_TRANSLATION",
                "translated_segments": [],
                "items": [],
                "error_code": "InvalidModelResponse",
                "_telemetry": translation_telemetry(),
            }
        partial = bool(failed_translations)
        return {
            "status": "available",
            "fallback_used": partial,
            "method": "GEMMA_FULL_SEGMENT_TRANSLATION",
            "translated_segments": translated_segments,
            "items": items,
            "partial": partial,
            "failed_segment_ids": [
                original_ids[target_id]
                for target_id, _ in failed_translations
            ],
            **(
                {"error_code": "PartialTranslationFailure"}
                if partial
                else {}
            ),
            "_telemetry": translation_telemetry(),
        }


def retrieve_with_query_expansion(
    *,
    retriever: Any,
    segment: dict[str, Any],
    context: list[dict[str, Any]],
    expansion: dict[str, Any] | None,
    base_candidates: list[dict[str, Any]] | None = None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, str]],
    list[dict[str, Any]],
]:
    """Retrieve raw first, then add search-only hits mapped to exact raw spans."""
    raw_text = segment["text"]
    candidates = (
        list(base_candidates)
        if base_candidates is not None
        else list(retriever.retrieve(raw_text=raw_text, context=context))
    )
    failures: list[dict[str, str]] = []
    raw_identities = {
        (
            candidate.get("collection"),
            candidate.get("entity_id"),
            candidate.get("canonical_ko"),
            candidate.get("canonical_en"),
            candidate.get("code"),
        )
        for candidate in candidates
        if isinstance(candidate, dict)
    }
    expanded_by_identity: dict[tuple[Any, ...], dict[str, Any]] = {}
    if not isinstance(expansion, dict) or expansion.get("status") != "available":
        return candidates, failures, []

    search_requests: list[dict[str, Any]] = []
    for item in expansion.get("items", []):
        if not isinstance(item, dict) or item.get("segment_id") != segment.get("id"):
            continue
        source_span = item.get("source_span")
        if not isinstance(source_span, dict):
            continue
        source_text = source_span.get("text")
        start = source_span.get("start_char")
        end = source_span.get("end_char")
        if (
            not isinstance(source_text, str)
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
            or end > len(raw_text)
            or raw_text[start:end] != source_text
        ):
            continue
        for search_term in item.get("search_terms_en", []):
            if not isinstance(search_term, str) or not search_term.strip():
                continue
            search_requests.append({
                "source_text": source_text,
                "start": start,
                "end": end,
                "search_term": search_term.strip(),
                "term_type": item.get("term_type"),
                "expansion_method": item.get(
                    "expansion_method", "GEMMA_CONTEXTUAL"
                ),
                "match_type": "gemma_query_expansion",
            })

    for translated in expansion.get("translated_segments", []):
        if (
            not isinstance(translated, dict)
            or translated.get("segment_id") != segment.get("id")
        ):
            continue
        translated_text = translated.get("translated_text_en")
        if not isinstance(translated_text, str) or not translated_text.strip():
            continue
        for search_term in _translation_search_phrases(translated_text):
            search_requests.append({
                "source_text": raw_text,
                "start": 0,
                "end": len(raw_text),
                "search_term": search_term,
                "term_type": None,
                "expansion_method": "GEMMA_FULL_SEGMENT_TRANSLATION",
                "match_type": "gemma_translation_search",
            })

    seen_queries: set[tuple[int, int, str]] = set()
    matched_translation_items: list[dict[str, Any]] = []
    for search_request in search_requests:
        source_text = search_request["source_text"]
        start = search_request["start"]
        end = search_request["end"]
        search_term = search_request["search_term"]
        query_key = (start, end, search_term.casefold())
        if query_key in seen_queries:
            continue
        seen_queries.add(query_key)
        try:
            expanded = retriever.retrieve(
                raw_text=search_term,
                context=context,
            )
        except Exception as error:
            failures.append(
                {
                    "segment_id": str(segment.get("id") or ""),
                    "error_code": type(error).__name__,
                }
            )
            continue
        matched = False
        for candidate in expanded:
            if not isinstance(candidate, dict):
                continue
            identity = (
                candidate.get("collection"),
                candidate.get("entity_id"),
                candidate.get("canonical_ko"),
                candidate.get("canonical_en"),
                candidate.get("code"),
            )
            if identity in raw_identities:
                continue
            matched = True
            mapped = dict(candidate)
            mapped.update(
                {
                    "source_text": source_text,
                    "start_char": start,
                    "end_char": end,
                    "source_match_type": candidate.get("match_type"),
                    "match_type": search_request["match_type"],
                    "review_status": "needs_review",
                    "search_term_en": search_term,
                    "query_term_type": search_request["term_type"],
                    "expansion_method": search_request["expansion_method"],
                }
            )
            existing = expanded_by_identity.get(identity)
            if existing is None or float(mapped.get("retrieval_score") or 0.0) > float(
                existing.get("retrieval_score") or 0.0
            ):
                expanded_by_identity[identity] = mapped
        if matched and search_request["match_type"] == "gemma_translation_search":
            matched_translation_items.append(
                {
                    "segment_id": segment.get("id"),
                    "source_span": {
                        "text": source_text,
                        "start_char": start,
                        "end_char": end,
                    },
                    "search_terms_en": [search_term],
                    "term_type": search_request["term_type"],
                    "expansion_method": search_request["expansion_method"],
                }
            )
    candidates.extend(expanded_by_identity.values())
    grouped_translation_items: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in matched_translation_items:
        source_span = item["source_span"]
        key = (
            item["segment_id"],
            source_span["start_char"],
            source_span["end_char"],
            source_span["text"],
            item["term_type"],
            item["expansion_method"],
        )
        grouped = grouped_translation_items.get(key)
        if grouped is None:
            grouped_translation_items[key] = {
                **item,
                "search_terms_en": list(item["search_terms_en"]),
            }
            continue
        for search_term in item["search_terms_en"]:
            if search_term not in grouped["search_terms_en"]:
                grouped["search_terms_en"].append(search_term)
    return candidates, failures, list(grouped_translation_items.values())


def run_query_expansion(
    expander: Any | None,
    segments: list[dict[str, Any]],
    *,
    covered_spans: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if expander is None:
        return {"status": "disabled", "fallback_used": False, "items": []}
    try:
        result = expander.expand(segments, covered_spans=covered_spans or [])
    except Exception as error:
        return {
            "status": "unavailable",
            "fallback_used": True,
            "items": [],
            "error_code": type(error).__name__,
        }
    if (
        not isinstance(result, dict)
        or result.get("status") not in {"available", "unavailable"}
        or not isinstance(result.get("items"), list)
    ):
        return {
            "status": "unavailable",
            "fallback_used": True,
            "items": [],
            "error_code": "InvalidExpansionContract",
        }
    normalized = dict(result)
    normalized["fallback_used"] = bool(
        result.get("fallback_used") or result.get("status") != "available"
    )
    if result.get("status") != "available":
        normalized["items"] = []
    return normalized


def unresolved_expansion_annotations(
    *,
    segment: dict[str, Any],
    expansion: dict[str, Any] | None,
    existing_annotations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(expansion, dict) or expansion.get("status") != "available":
        return []
    matched_spans = {
        (
            annotation.get("source_span", {}).get("start_char"),
            annotation.get("source_span", {}).get("end_char"),
            annotation.get("source_span", {}).get("text"),
        )
        for annotation in existing_annotations
        if annotation.get("type")
        in {"medical_term_candidate", "diagnosis_term_candidate"}
        and annotation.get("candidates")
    }
    unresolved: list[dict[str, Any]] = []
    for item in expansion.get("items", []):
        if not isinstance(item, dict) or item.get("segment_id") != segment.get("id"):
            continue
        span = item.get("source_span")
        if not isinstance(span, dict):
            continue
        key = (span.get("start_char"), span.get("end_char"), span.get("text"))
        if key in matched_spans:
            continue
        unresolved.append(
            {
                "type": "unresolved_medical_term",
                "source_span": dict(span),
                "candidates": [],
                "needs_review": True,
                "search_terms_en": list(item.get("search_terms_en") or []),
                "term_type": item.get("term_type"),
                "reason": "DICTIONARY_NO_MATCH",
            }
        )
    return unresolved

