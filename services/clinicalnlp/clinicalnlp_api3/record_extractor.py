from __future__ import annotations

import copy
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any
from urllib.request import Request, urlopen

from .clinical_llm import (
    ClinicalLlmClient,
    ClinicalLlmLengthLimit,
    InvalidClinicalLlmOutput,
    OllamaCloudClinicalLlmClient,
)
from .clinical_record_pipeline import (
    extract_clinical_record,
    select_clinical_record_candidate,
)
from .model_output_contracts import (
    candidate_adjudication_response_format,
    clinical_record_response_format,
    draft_normalization_response_format,
)
from .draft_normalization import (
    build_draft_normalization_plan,
    ground_model_draft_suggestions,
)
from .compact_record_v3 import (
    compact_record_response_format,
    validate_compact_record,
)
from .compact_record_lean import (
    FIELD_GROUPS,
    MAX_CHUNKS,
    MAX_LOGICAL_LLM_CALLS,
    MAX_SEGMENTS_PER_CHUNK,
    MAX_SPLIT_DEPTH,
    PROMPT_VERSION as LEAN_PROMPT_VERSION,
    SCHEMA_VERSION as LEAN_SCHEMA_VERSION,
    fact_chunk_response_format,
    field_response_format,
    lean_record_response_format,
    merge_chunk_facts,
    minimal_candidate_projection,
    validate_lean_record,
)


PROJECT_ROOT = Path(__file__).parents[1]
DEFAULT_PROMPT = PROJECT_ROOT / "prompts" / "clinical_record_extraction_v2.txt"
DEFAULT_CANDIDATE_PROMPT = (
    PROJECT_ROOT / "prompts" / "candidate_adjudication_v1.txt"
)
DEFAULT_DRAFT_NORMALIZATION_PROMPT = (
    PROJECT_ROOT / "prompts" / "draft_normalization_v1.txt"
)
DEFAULT_COMPACT_OUTPUT_PROMPT = (
    PROJECT_ROOT / "prompts" / "compact_record_output_v3.txt"
)
DEFAULT_LEAN_OUTPUT_PROMPT = (
    PROJECT_ROOT / "prompts" / "compact_record_output_v3_1_lean.txt"
)
DEFAULT_LEAN_FACT_PROMPT = PROJECT_ROOT / "prompts" / "compact_fact_output_v1.txt"
DEFAULT_LEAN_FIELDS_PROMPT = PROJECT_ROOT / "prompts" / "compact_fields_output_v1.txt"
DEFAULT_MAX_OUTPUT_TOKENS = 8192
MAX_CANDIDATES_PER_ANNOTATION = 5
MAX_MODEL_CANDIDATES_PER_ANNOTATION = 3
MAX_MODEL_CANDIDATE_ANNOTATIONS = 16
CLINICAL_PROMPT_VERSION = "clinical-record-extraction-v2.12"
CANDIDATE_PROMPT_VERSION = "candidate-adjudication-v1"
DRAFT_NORMALIZATION_PROMPT_VERSION = "draft-normalization-v1"
COMPACT_PROMPT_VERSION = "clinical-record-compact-v3.2"
LEAN_REQUEST_DEADLINE_SECONDS = 620.0


class _LeanCallBudget:
    def __init__(self, maximum: int):
        self.maximum = maximum
        self.used = 0
        self._lock = threading.Lock()

    def reserve(self) -> int:
        with self._lock:
            if self.used >= self.maximum:
                raise RuntimeError("Compact v3.1 Lean logical call budget exhausted")
            self.used += 1
            return self.used


class _LeanTelemetry:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = threading.local()
        self.values: dict[str, Any] = {
            "contract_version": LEAN_SCHEMA_VERSION,
            "generation_route": "single",
            "fact_chunk_count": 0,
            "field_group_call_count": 0,
            "llm_call_count": 0,
            "provider_call_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "length_fallback_count": 0,
            "repair_count": 0,
            "regeneration_count": 0,
            "network_retry_count": 0,
            "failed_segment_count": 0,
        }

    def add_call(self, diagnostics: dict[str, Any] | None = None) -> None:
        diagnostics = diagnostics or {}
        self._thread.last_call = copy.deepcopy(diagnostics)
        with self._lock:
            provider_calls = diagnostics.get("provider_call_count")
            self.values["llm_call_count"] += (
                provider_calls
                if isinstance(provider_calls, int)
                and not isinstance(provider_calls, bool)
                and provider_calls > 0
                else 1
            )
            for key in (
                "provider_call_count",
                "input_tokens",
                "output_tokens",
                "repair_count",
                "regeneration_count",
                "network_retry_count",
            ):
                value = diagnostics.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    self.values[key] += value

    def increment(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self.values[key] = int(self.values.get(key, 0)) + amount

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.values[key] = value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.values)

    def last_call(self) -> dict[str, Any]:
        value = getattr(self._thread, "last_call", None)
        return copy.deepcopy(value) if isinstance(value, dict) else {}


def _json_candidates(
    output: str,
    required_key: str,
    required_type: type,
) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    cleaned = output.replace("```json", "").replace("```JSON", "").replace("```", "")
    for position, character in enumerate(cleaned):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(cleaned[position:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(
            value.get(required_key), required_type
        ):
            candidates.append(value)
    return candidates


class LlamaServerClinicalExtractor:
    """Extract a grounded clinical record with the loaded llama-server."""

    def __init__(
        self,
        base_url: str,
        *,
        model_name: str = "gemma-4-E4B",
        context_size: int = 8192,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        timeout: float = 600,
        api2_root: Path | None = None,
        prompt_path: Path | None = None,
        candidate_prompt_path: Path | None = None,
        draft_normalization_prompt_path: Path | None = None,
        compact_output_prompt_path: Path | None = None,
        lean_output_prompt_path: Path | None = None,
        lean_fact_prompt_path: Path | None = None,
        lean_fields_prompt_path: Path | None = None,
        llm_client: ClinicalLlmClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.context_size = context_size
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
        self.llm_client = llm_client
        # Kept only so older launch code does not break. API2 now lives in this
        # package and this legacy path is never read or imported.
        self.api2_root = Path(api2_root) if api2_root is not None else None
        self.prompt_path = Path(prompt_path or DEFAULT_PROMPT)
        self.candidate_prompt_path = Path(
            candidate_prompt_path or DEFAULT_CANDIDATE_PROMPT
        )
        self.draft_normalization_prompt_path = Path(
            draft_normalization_prompt_path or DEFAULT_DRAFT_NORMALIZATION_PROMPT
        )
        self.compact_output_prompt_path = Path(
            compact_output_prompt_path or DEFAULT_COMPACT_OUTPUT_PROMPT
        )
        self.lean_output_prompt_path = Path(
            lean_output_prompt_path or DEFAULT_LEAN_OUTPUT_PROMPT
        )
        self.lean_fact_prompt_path = Path(
            lean_fact_prompt_path or DEFAULT_LEAN_FACT_PROMPT
        )
        self.lean_fields_prompt_path = Path(
            lean_fields_prompt_path or DEFAULT_LEAN_FIELDS_PROMPT
        )

    @classmethod
    def from_environment(cls) -> "LlamaServerClinicalExtractor":
        provider = os.environ.get("CLINICAL_LLM_PROVIDER", "local").strip().casefold()
        if provider not in {"local", "ollama_cloud"}:
            raise ValueError(f"Unsupported CLINICAL_LLM_PROVIDER: {provider}")
        cloud = provider == "ollama_cloud"
        base_url = (
            os.environ.get("OLLAMA_BASE_URL", "https://ollama.com")
            if cloud
            else os.environ.get(
                "CLINICALNLP_API2_GEMMA_URL",
                os.environ.get(
                    "CLINICALNLP_API3_GEMMA_URL", "http://127.0.0.1:8081"
                ),
            )
        )
        model_name = (
            os.environ.get("OLLAMA_MODEL", "gemma4:31b")
            if cloud
            else os.environ.get(
                "CLINICALNLP_API2_GEMMA_MODEL",
                os.environ.get("CLINICALNLP_API3_GEMMA_MODEL", "gemma-4-E4B"),
            )
        )
        max_output_tokens = int(
            os.environ.get(
                "CLINICALNLP_GEMMA_MAX_TOKENS",
                str(DEFAULT_MAX_OUTPUT_TOKENS),
            )
        )
        timeout = float(
            os.environ.get(
                "OLLAMA_TIMEOUT" if cloud else "CLINICALNLP_API2_GEMMA_TIMEOUT",
                (
                    "240"
                    if cloud
                    else os.environ.get("CLINICALNLP_API3_GEMMA_TIMEOUT", "600")
                ),
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
        extractor = cls(
            base_url,
            model_name=model_name,
            context_size=int(
                os.environ.get(
                    "CLINICALNLP_API2_CONTEXT",
                    os.environ.get("CLINICALNLP_API3_CONTEXT", "8192"),
                )
            ),
            max_output_tokens=max_output_tokens,
            timeout=timeout,
            llm_client=llm_client,
        )
        if not cloud:
            extractor.require_healthy()
        return extractor

    def require_healthy(self) -> None:
        try:
            with urlopen(f"{self.base_url}/health", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise ValueError(
                f"Gemma server is unavailable at {self.base_url}"
            ) from error
        if payload.get("status") != "ok":
            raise ValueError(f"Gemma server is not ready at {self.base_url}")

    def _generate_chunk(
        self,
        task: str,
        chunk: dict[str, Any],
        *,
        response_format: dict[str, Any],
        required_key: str,
        required_type: type,
        output_label: str,
    ) -> list[dict[str, Any]]:
        if self.llm_client is not None:
            return [
                self.llm_client.generate_json(
                    system_prompt=task,
                    user_payload=chunk,
                    response_format=response_format,
                    output_label=output_label,
                )
            ]
        request_payload = json.dumps(
            {
                "model": self.model_name,
                "messages": [
                    {"role": "system", "content": task},
                    {"role": "user", "content": json.dumps(chunk, ensure_ascii=False)},
                ],
                "temperature": 0,
                "max_tokens": min(
                    self.max_output_tokens,
                    max(1024, self.context_size // 2),
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
        content = choice["message"]["content"]
        if not isinstance(content, str):
            raise ValueError(f"Gemma returned no {output_label} text content")
        candidates = _json_candidates(content, required_key, required_type)
        if not candidates:
            preview = " ".join(content.strip().split())[:240]
            usage = result.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            diagnostics = [
                f"finish_reason={choice.get('finish_reason', '<missing>')}",
                f"prompt_tokens={usage.get('prompt_tokens', '<missing>')}",
                f"completion_tokens={usage.get('completion_tokens', '<missing>')}",
            ]
            raise ValueError(
                f"Gemma returned no valid {output_label} JSON; "
                f"{', '.join(diagnostics)}; "
                f"response preview: {preview or '<empty>'}"
            )
        return candidates

    @staticmethod
    def _model_payload(payload: dict[str, Any]) -> dict[str, Any]:
        segments: list[dict[str, Any]] = []
        for segment in payload.get("segments", []):
            raw_text = segment.get("raw_text", segment.get("text", ""))
            corrected_text = segment.get("corrected_text", segment.get("text", ""))
            compact = {
                "id": segment.get("id"),
                "start": segment.get("start"),
                "end": segment.get("end"),
                "raw_text": raw_text,
            }
            if corrected_text != raw_text:
                compact["corrected_text"] = corrected_text
            translated_text = segment.get("translated_text_en")
            if isinstance(translated_text, str) and translated_text.strip():
                compact["translated_text_en"] = translated_text.strip()
            segments.append(compact)
        return {"segments": segments}

    @staticmethod
    def _response_format(chunk: dict[str, Any]) -> dict[str, Any]:
        segment_ids = [
            segment.get("id")
            for segment in chunk.get("segments", [])
            if isinstance(segment, dict) and isinstance(segment.get("id"), str)
        ]
        return clinical_record_response_format(segment_ids)

    @staticmethod
    def _candidate_payload(payload: dict[str, Any]) -> dict[str, Any]:
        candidate_keys = {
            "collection",
            "entity_id",
            "canonical_ko",
            "canonical_en",
            "entity_type",
            "code",
            "code_display",
            "match_type",
            "retrieval_score",
            "retrieved_text",
            "provenance",
            "dictionary_version",
        }
        segments: list[dict[str, Any]] = []
        for segment in payload.get("segments", []):
            raw_text = segment.get("raw_text", segment.get("text", ""))
            corrected_text = segment.get("corrected_text", segment.get("text", ""))
            compact: dict[str, Any] = {
                "id": segment.get("id"),
                "start": segment.get("start"),
                "end": segment.get("end"),
                "raw_text": raw_text,
            }
            if corrected_text != raw_text:
                compact["corrected_text"] = corrected_text
            translated_text = segment.get("translated_text_en")
            if isinstance(translated_text, str) and translated_text.strip():
                compact["translated_text_en"] = translated_text.strip()

            annotations: list[dict[str, Any]] = []
            for position, annotation in enumerate(segment.get("annotations", [])):
                if (
                    not isinstance(annotation, dict)
                    or not annotation.get("needs_review")
                    or annotation.get("type")
                    not in {
                        "medical_term_candidate",
                        "diagnosis_term_candidate",
                    }
                ):
                    continue
                annotation_index = annotation.get("annotation_index", position)
                if not isinstance(annotation_index, int):
                    continue
                candidates = [
                    {
                        key: value
                        for key, value in candidate.items()
                        if key in candidate_keys
                    }
                    for candidate in annotation.get("candidates", [])
                    if isinstance(candidate, dict)
                    and isinstance(candidate.get("entity_id"), str)
                ][:MAX_CANDIDATES_PER_ANNOTATION]
                if not candidates:
                    continue
                compact_annotation = {
                    "annotation_index": annotation_index,
                    "type": annotation["type"],
                    "source_span": annotation.get("source_span", {}),
                    "candidates": candidates,
                }
                if annotation.get("term_type"):
                    compact_annotation["term_type"] = annotation["term_type"]
                annotations.append(compact_annotation)
            if annotations:
                compact["annotations"] = annotations
            segments.append(compact)
        return {"segments": segments}

    def generate_compact_record(
        self,
        payload: dict[str, Any],
        candidate_snapshots: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Generate one validated Compact v3 clinical record."""

        base_prompt = self.prompt_path.read_text(encoding="utf-8")
        extraction_rules = base_prompt.split("Value V is", 1)[0].rstrip()
        output_contract = self.compact_output_prompt_path.read_text(
            encoding="utf-8"
        )
        model_payload = self._candidate_payload(payload)
        model_payload["candidate_snapshots"] = list(candidate_snapshots.values())
        segment_ids = [
            segment.get("id")
            for segment in model_payload.get("segments", [])
            if isinstance(segment, dict) and isinstance(segment.get("id"), str)
        ]
        generated = self._generate_chunk(
            f"{extraction_rules}\n\n{output_contract}",
            model_payload,
            response_format=compact_record_response_format(
                segment_ids,
                candidate_snapshots,
            ),
            required_key="fields",
            required_type=dict,
            output_label="Compact v3 clinical record",
        )[0]
        return {
            "prompt_version": COMPACT_PROMPT_VERSION,
            "record": generated,
            "validation": validate_compact_record(
                generated,
                segment_ids=segment_ids,
                candidate_snapshots=candidate_snapshots,
            ),
        }

    @staticmethod
    def _estimated_tokens(value: Any) -> int:
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return max(1, (len(serialized) + 3) // 4)

    def _lean_call(
        self,
        *,
        prompt: str,
        payload: dict[str, Any],
        response_format: dict[str, Any],
        required_key: str,
        required_type: type,
        output_label: str,
        budget: _LeanCallBudget,
        telemetry: _LeanTelemetry,
        deadline: float,
    ) -> dict[str, Any]:
        if deadline - time.monotonic() < min(
            float(self.timeout),
            LEAN_REQUEST_DEADLINE_SECONDS,
        ):
            raise TimeoutError("Compact v3.1 Lean request deadline is insufficient")
        diagnostics: dict[str, Any] = {}
        try:
            if self.llm_client is not None:
                if isinstance(self.llm_client, OllamaCloudClinicalLlmClient):
                    return self.llm_client.generate_json(
                        system_prompt=prompt,
                        user_payload=payload,
                        response_format=response_format,
                        output_label=output_label,
                        call_reserver=budget.reserve,
                    )
                budget.reserve()
                return self.llm_client.generate_json(
                    system_prompt=prompt,
                    user_payload=payload,
                    response_format=response_format,
                    output_label=output_label,
                )
            budget.reserve()
            try:
                return self._generate_chunk(
                    prompt,
                    payload,
                    response_format=response_format,
                    required_key=required_key,
                    required_type=required_type,
                    output_label=output_label,
                )[0]
            except ValueError as error:
                if "finish_reason=length" in str(error):
                    raise ClinicalLlmLengthLimit(str(error)) from error
                raise
        finally:
            reader = getattr(self.llm_client, "last_diagnostics", None)
            if callable(reader):
                value = reader()
                diagnostics = value if isinstance(value, dict) else {}
            telemetry.add_call(diagnostics)

    @staticmethod
    def _lean_segment_chunks(
        segments: list[dict[str, Any]],
        snapshots: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        chunks: list[dict[str, Any]] = []
        current: list[dict[str, Any]] = []
        for segment in segments:
            proposal = [*current, segment]
            proposal_ids = [
                str(item.get("id"))
                for item in proposal
                if isinstance(item.get("id"), str)
            ]
            payload = {
                "segments": proposal,
                "candidate_snapshots": minimal_candidate_projection(
                    snapshots,
                    segment_ids=proposal_ids,
                ),
            }
            oversized = (
                len(proposal) > MAX_SEGMENTS_PER_CHUNK
                or LlamaServerClinicalExtractor._estimated_tokens(payload) > 3500
            )
            if current and oversized:
                chunks.append({"owned_segments": current})
                current = [segment]
            else:
                current = proposal
        if current:
            chunks.append({"owned_segments": current})

        overflow_ids: list[str] = []
        if len(chunks) > MAX_CHUNKS:
            for chunk in chunks[MAX_CHUNKS:]:
                overflow_ids.extend(
                    str(item.get("id"))
                    for item in chunk["owned_segments"]
                    if isinstance(item.get("id"), str)
                )
            chunks = chunks[:MAX_CHUNKS]
        for index, chunk in enumerate(chunks):
            owned = chunk["owned_segments"]
            context: list[dict[str, Any]] = []
            if index and chunks[index - 1]["owned_segments"]:
                overlap = copy.deepcopy(chunks[index - 1]["owned_segments"][-1])
                overlap["context_only"] = True
                context.append(overlap)
            chunk["segments"] = context + [
                {**copy.deepcopy(item), "context_only": False} for item in owned
            ]
        return chunks, overflow_ids

    def _extract_lean_fact_chunk(
        self,
        *,
        extraction_rules: str,
        chunk_prompt: str,
        owned_segments: list[dict[str, Any]],
        context_segment: dict[str, Any] | None,
        candidate_snapshots: dict[str, dict[str, Any]],
        budget: _LeanCallBudget,
        telemetry: _LeanTelemetry,
        deadline: float,
        depth: int = 0,
    ) -> tuple[list[dict[str, Any]], list[str], list[dict[str, Any]]]:
        owned_ids = [
            str(segment.get("id"))
            for segment in owned_segments
            if isinstance(segment.get("id"), str)
        ]
        model_segments: list[dict[str, Any]] = []
        if context_segment is not None:
            overlap = copy.deepcopy(context_segment)
            overlap["context_only"] = True
            model_segments.append(overlap)
        model_segments.extend(
            {**copy.deepcopy(segment), "context_only": False}
            for segment in owned_segments
        )
        payload = {
            "owned_segment_ids": owned_ids,
            "segments": model_segments,
            "candidate_snapshots": minimal_candidate_projection(
                candidate_snapshots,
                segment_ids=owned_ids,
            ),
        }
        started = time.perf_counter()
        try:
            generated = self._lean_call(
                prompt=f"{extraction_rules}\n\n{chunk_prompt}",
                payload=payload,
                response_format=fact_chunk_response_format(),
                required_key="facts",
                required_type=dict,
                output_label="Compact v3.1 fact chunk",
                budget=budget,
                telemetry=telemetry,
                deadline=deadline,
            )
            facts = generated.get("facts")
            facts = facts if isinstance(facts, dict) else {}
            for fact in facts.values():
                fact_segments = fact.get("segments") if isinstance(fact, dict) else []
                if any(str(segment_id) not in owned_ids for segment_id in fact_segments):
                    raise InvalidClinicalLlmOutput(
                        "fact chunk used a context-only or unknown source segment"
                    )
            digest = hashlib.sha256(
                json.dumps(generated, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            call_diagnostics = telemetry.last_call()
            audit = [{
                "segment_ids": owned_ids,
                "status": "completed",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "output_sha256": digest,
                "validation_codes": [],
                "provider_call_count": call_diagnostics.get("provider_call_count", 1),
                "input_tokens": call_diagnostics.get("input_tokens", 0),
                "output_tokens": call_diagnostics.get("output_tokens", 0),
                "done_reason": call_diagnostics.get("done_reason"),
            }]
            return [facts], [], audit
        except ClinicalLlmLengthLimit as error:
            telemetry.increment("length_fallback_count")
            if len(owned_segments) <= 1 or depth >= MAX_SPLIT_DEPTH:
                call_diagnostics = telemetry.last_call()
                return [], owned_ids, [{
                    "segment_ids": owned_ids,
                    "status": "failed",
                    "error_code": "ChunkTooLarge",
                    "validation_codes": ["ChunkTooLarge"],
                    "detail": str(error),
                    "provider_call_count": call_diagnostics.get("provider_call_count", 1),
                    "input_tokens": call_diagnostics.get("input_tokens", 0),
                    "output_tokens": call_diagnostics.get("output_tokens", 0),
                    "done_reason": call_diagnostics.get("done_reason", "length"),
                }]
            middle = len(owned_segments) // 2
            left = owned_segments[:middle]
            right = owned_segments[middle:]
            left_facts, left_failed, left_audit = self._extract_lean_fact_chunk(
                extraction_rules=extraction_rules,
                chunk_prompt=chunk_prompt,
                owned_segments=left,
                context_segment=context_segment,
                candidate_snapshots=candidate_snapshots,
                budget=budget,
                telemetry=telemetry,
                deadline=deadline,
                depth=depth + 1,
            )
            right_context = left[-1] if left else context_segment
            right_facts, right_failed, right_audit = self._extract_lean_fact_chunk(
                extraction_rules=extraction_rules,
                chunk_prompt=chunk_prompt,
                owned_segments=right,
                context_segment=right_context,
                candidate_snapshots=candidate_snapshots,
                budget=budget,
                telemetry=telemetry,
                deadline=deadline,
                depth=depth + 1,
            )
            return (
                left_facts + right_facts,
                left_failed + right_failed,
                left_audit + right_audit,
            )
        except Exception as error:
            call_diagnostics = telemetry.last_call()
            return [], owned_ids, [{
                "segment_ids": owned_ids,
                "status": "failed",
                "error_code": type(error).__name__,
                "validation_codes": [type(error).__name__],
                "detail": str(error),
                "provider_call_count": call_diagnostics.get("provider_call_count", 1),
                "input_tokens": call_diagnostics.get("input_tokens", 0),
                "output_tokens": call_diagnostics.get("output_tokens", 0),
                "done_reason": call_diagnostics.get("done_reason"),
            }]

    def _generate_lean_fields(
        self,
        *,
        extraction_rules: str,
        fields_prompt: str,
        segments: list[dict[str, Any]],
        facts: dict[str, Any],
        candidate_snapshots: dict[str, dict[str, Any]],
        failed_segment_ids: list[str],
        budget: _LeanCallBudget,
        telemetry: _LeanTelemetry,
        deadline: float,
    ) -> tuple[dict[str, Any], list[str]]:
        supported_segment_ids = {
            str(segment_id)
            for fact in facts.values()
            if isinstance(fact, dict)
            for segment_id in fact.get("segments", [])
        }
        supported_segments = [
            segment
            for segment in segments
            if str(segment.get("id") or "") in supported_segment_ids
        ]
        base_payload = {
            "segments": supported_segments,
            "facts": facts,
            "candidate_snapshots": minimal_candidate_projection(
                candidate_snapshots,
                segment_ids=supported_segment_ids,
            ),
            "failed_segment_ids": failed_segment_ids,
        }
        prompt = f"{extraction_rules}\n\n{fields_prompt}"
        try:
            generated = self._lean_call(
                prompt=prompt,
                payload={**base_payload, "requested_fields": list(FIELD_GROUPS[0] + FIELD_GROUPS[1] + FIELD_GROUPS[2])},
                response_format=field_response_format(),
                required_key="fields",
                required_type=dict,
                output_label="Compact v3.1 fields",
                budget=budget,
                telemetry=telemetry,
                deadline=deadline,
            )
            fields = generated.get("fields")
            return (fields if isinstance(fields, dict) else {}), []
        except ClinicalLlmLengthLimit:
            telemetry.increment("length_fallback_count")
        except Exception:
            return {}, list(FIELD_GROUPS[0] + FIELD_GROUPS[1] + FIELD_GROUPS[2])

        fields: dict[str, Any] = {}
        failed_fields: list[str] = []
        for group in FIELD_GROUPS:
            try:
                telemetry.increment("field_group_call_count")
                generated = self._lean_call(
                    prompt=prompt,
                    payload={**base_payload, "requested_fields": list(group)},
                    response_format=field_response_format(group),
                    required_key="fields",
                    required_type=dict,
                    output_label="Compact v3.1 field group",
                    budget=budget,
                    telemetry=telemetry,
                    deadline=deadline,
                )
                group_fields = generated.get("fields")
                if isinstance(group_fields, dict):
                    fields.update(group_fields)
            except Exception:
                failed_fields.extend(group)
        return fields, failed_fields

    def generate_compact_record_lean(
        self,
        payload: dict[str, Any],
        candidate_snapshots: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Generate sparse Compact v3.1 output with a bounded long-input path."""

        started = time.perf_counter()
        deadline = time.monotonic() + LEAN_REQUEST_DEADLINE_SECONDS
        budget = _LeanCallBudget(MAX_LOGICAL_LLM_CALLS)
        telemetry = _LeanTelemetry()
        base_prompt = self.prompt_path.read_text(encoding="utf-8")
        extraction_rules = base_prompt.split("Value V is", 1)[0].rstrip()
        lean_prompt = self.lean_output_prompt_path.read_text(encoding="utf-8")
        fact_prompt = self.lean_fact_prompt_path.read_text(encoding="utf-8")
        fields_prompt = self.lean_fields_prompt_path.read_text(encoding="utf-8")
        model_payload = self._model_payload(payload)
        segments = model_payload.get("segments")
        segments = segments if isinstance(segments, list) else []
        segment_ids = [
            str(segment.get("id"))
            for segment in segments
            if isinstance(segment, dict) and isinstance(segment.get("id"), str)
        ]
        minimal_candidates = minimal_candidate_projection(candidate_snapshots)
        single_payload = {
            "segments": segments,
            "candidate_snapshots": minimal_candidates,
        }
        estimated_facts = min(
            96,
            max(len(segments) * 3, len(minimal_candidates), 1),
        )
        estimated_output_tokens = 300 + estimated_facts * 45 + 12 * 110
        single_route = (
            len(segments) <= MAX_SEGMENTS_PER_CHUNK
            and self._estimated_tokens(single_payload) <= 3500
            and estimated_output_tokens <= 4096
        )
        if single_route:
            try:
                generated = self._lean_call(
                    prompt=f"{extraction_rules}\n\n{lean_prompt}",
                    payload=single_payload,
                    response_format=lean_record_response_format(),
                    required_key="fields",
                    required_type=dict,
                    output_label="Compact v3.1 Lean clinical record",
                    budget=budget,
                    telemetry=telemetry,
                    deadline=deadline,
                )
                validation = validate_lean_record(
                    generated,
                    segment_ids=segment_ids,
                    candidate_snapshots=candidate_snapshots,
                )
                telemetry.set("elapsed_ms", round((time.perf_counter() - started) * 1000, 3))
                return {
                    "prompt_version": LEAN_PROMPT_VERSION,
                    "record": generated,
                    "validation": validation,
                    "generation": telemetry.snapshot(),
                    "audit": {"chunks": [], "fact_id_map": []},
                }
            except ClinicalLlmLengthLimit:
                telemetry.increment("length_fallback_count")

        telemetry.set("generation_route", "chunked")
        chunks, overflow_ids = self._lean_segment_chunks(
            [segment for segment in segments if isinstance(segment, dict)],
            candidate_snapshots,
        )
        telemetry.set("fact_chunk_count", len(chunks))
        failed_segment_ids = list(overflow_ids)
        chunk_audit: list[dict[str, Any]] = []
        ordered_fact_groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {}
            for index, chunk in enumerate(chunks, start=1):
                context = None
                if index > 1 and chunks[index - 2]["owned_segments"]:
                    context = chunks[index - 2]["owned_segments"][-1]
                future = executor.submit(
                    self._extract_lean_fact_chunk,
                    extraction_rules=extraction_rules,
                    chunk_prompt=fact_prompt,
                    owned_segments=chunk["owned_segments"],
                    context_segment=context,
                    candidate_snapshots=candidate_snapshots,
                    budget=budget,
                    telemetry=telemetry,
                    deadline=deadline,
                )
                futures[future] = index
            for future in as_completed(futures):
                index = futures[future]
                fact_groups, failed_ids, audit_entries = future.result()
                ordered_fact_groups[index].extend(fact_groups)
                failed_segment_ids.extend(failed_ids)
                for entry in audit_entries:
                    chunk_audit.append({"chunk_id": f"chunk_{index:02d}", **entry})

        flattened: list[tuple[int, Mapping[str, Any]]] = []
        chunk_number = 0
        for index in sorted(ordered_fact_groups):
            for facts in ordered_fact_groups[index]:
                chunk_number += 1
                flattened.append((chunk_number, facts))
        merged_facts, fact_id_map = merge_chunk_facts(flattened)
        successful_chunk_count = len(flattened)
        fields, failed_field_ids = self._generate_lean_fields(
            extraction_rules=extraction_rules,
            fields_prompt=fields_prompt,
            segments=[segment for segment in segments if isinstance(segment, dict)],
            facts=merged_facts,
            candidate_snapshots=candidate_snapshots,
            failed_segment_ids=failed_segment_ids,
            budget=budget,
            telemetry=telemetry,
            deadline=deadline,
        )
        technical_status = (
            "failed"
            if segments and not successful_chunk_count
            else "partial"
            if failed_segment_ids or failed_field_ids
            else "completed"
        )
        record = {
            "schema_version": LEAN_SCHEMA_VERSION,
            "facts": merged_facts,
            "fields": fields,
        }
        validation = validate_lean_record(
            record,
            segment_ids=segment_ids,
            candidate_snapshots=candidate_snapshots,
            failed_segment_ids=failed_segment_ids,
            impacted_field_ids=failed_field_ids,
            technical_status=technical_status,
        )
        telemetry.set("failed_segment_count", len(set(failed_segment_ids)))
        telemetry.set("elapsed_ms", round((time.perf_counter() - started) * 1000, 3))
        return {
            "prompt_version": LEAN_PROMPT_VERSION,
            "record": record,
            "validation": validation,
            "generation": telemetry.snapshot(),
            "audit": {
                "chunks": sorted(chunk_audit, key=lambda item: (item["chunk_id"], str(item.get("segment_ids")))),
                "fact_id_map": fact_id_map,
            },
        }

    def compare_compact_record(
        self,
        payload: dict[str, Any],
        candidate_snapshots: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Backward-compatible entry point for the comparison-only mode."""

        return self.generate_compact_record(payload, candidate_snapshots)

    @staticmethod
    def _has_candidate_annotations(payload: dict[str, Any]) -> bool:
        return any(
            segment.get("annotations")
            for segment in payload.get("segments", [])
            if isinstance(segment, dict)
        )

    @staticmethod
    def _is_resolver_generated_annotation(annotation: dict[str, Any]) -> bool:
        for candidate in annotation.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            match_type = str(candidate.get("match_type") or "").casefold()
            provenance = candidate.get("provenance")
            source = (
                str(provenance.get("source") or "").casefold()
                if isinstance(provenance, dict)
                else ""
            )
            if match_type or source:
                return True
        return False

    @classmethod
    def _candidate_adjudication_payload(
        cls,
        candidate_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        segments: list[dict[str, Any]] = []
        policy_decisions: list[dict[str, Any]] = []
        annotation_count = 0
        for source_segment in candidate_payload.get("segments", []):
            if not isinstance(source_segment, dict):
                continue
            segment = {
                key: copy.deepcopy(value)
                for key, value in source_segment.items()
                if key != "annotations"
            }
            annotations: list[dict[str, Any]] = []
            for annotation in source_segment.get("annotations", []):
                if not isinstance(annotation, dict):
                    continue
                resolver_generated = cls._is_resolver_generated_annotation(
                    annotation
                )
                within_budget = (
                    annotation_count < MAX_MODEL_CANDIDATE_ANNOTATIONS
                )
                if resolver_generated or not within_budget:
                    policy_decisions.append(
                        {
                            "segment_id": source_segment.get("id"),
                            "annotation_index": annotation.get("annotation_index"),
                            "action": "needs_review",
                            "selected_candidate_ids": [],
                            "confidence": None,
                            "reason": (
                                "Resolver-generated candidate requires explicit "
                                "clinician review"
                                if resolver_generated
                                else "Candidate adjudication was deferred by the "
                                "bounded model-input policy"
                            ),
                        }
                    )
                    continue
                compact_annotation = copy.deepcopy(annotation)
                compact_annotation["candidates"] = compact_annotation.get(
                    "candidates", []
                )[:MAX_MODEL_CANDIDATES_PER_ANNOTATION]
                annotations.append(compact_annotation)
                annotation_count += 1
            if annotations:
                segment["annotations"] = annotations
            segments.append(segment)
        return {"segments": segments}, policy_decisions

    @staticmethod
    def _supplemental_fields(
        model_record: dict[str, Any],
        payload: dict[str, Any],
    ) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        segments = {
            segment.get("id"): segment
            for segment in payload.get("segments", [])
            if isinstance(segment, dict)
        }
        fields: dict[str, list[dict[str, Any]]] = {}
        warnings: list[str] = []
        for field_name in (
            "review_of_systems",
            "physical_examination",
            "impression",
            "treatment_plan",
            "outcome",
        ):
            grounded: list[dict[str, Any]] = []
            values = model_record.get(field_name, [])
            if field_name in {
                "review_of_systems",
                "physical_examination",
                "impression",
                "treatment_plan",
                "outcome",
            }:
                if field_name not in model_record or isinstance(values, dict):
                    continue
            if not isinstance(values, list):
                values = []
            for value in values:
                if not isinstance(value, dict):
                    warnings.append(f"invalid {field_name} item was discarded")
                    continue
                raw_value = value.get("raw_value")
                status = value.get("status")
                evidence = value.get("evidence")
                segment_id = (
                    evidence.get("source_segment_id")
                    if isinstance(evidence, dict)
                    and set(evidence) == {"source_segment_id"}
                    else None
                )
                segment = segments.get(segment_id)
                if (
                    not isinstance(raw_value, str)
                    or not raw_value.strip()
                    or status not in {"confirmed", "needs_confirmation"}
                    or segment is None
                ):
                    warnings.append(f"ungrounded {field_name} item was discarded")
                    continue
                source_texts = {
                    str(segment.get(key) or "")
                    for key in ("text", "raw_text", "corrected_text")
                }
                compact_value = "".join(raw_value.split())
                if not any(
                    compact_value in "".join(source_text.split())
                    for source_text in source_texts
                ):
                    warnings.append(f"unmatched {field_name} item was discarded")
                    continue
                grounded.append(
                    {
                        "raw_value": raw_value.strip(),
                        "status": status,
                        "evidence": {
                            "text": segment.get("text"),
                            "start": segment.get("start"),
                            "end": segment.get("end"),
                        },
                    }
                )
            fields[field_name] = grounded
        return fields, warnings

    @staticmethod
    def _sanitize_model_record(
        model_record: dict[str, Any],
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        generic_values = {"약", "증상", "병", "진단", "수술"}
        segments = [
            segment
            for segment in payload.get("segments", [])
            if isinstance(segment, dict)
        ]
        segment_positions = {
            segment.get("id"): position for position, segment in enumerate(segments)
        }
        discarded = object()
        warnings: list[str] = []

        def segment_id_for(value: dict[str, Any]) -> Any:
            evidence = value.get("evidence")
            if not isinstance(evidence, dict):
                return None
            source_segment_id = evidence.get("source_segment_id")
            if source_segment_id in segment_positions:
                return source_segment_id

            start, end = evidence.get("start"), evidence.get("end")
            timing_matches = [
                segment.get("id")
                for segment in segments
                if start is not None
                and end is not None
                and segment.get("start") == start
                and segment.get("end") == end
            ]
            if len(timing_matches) == 1:
                return timing_matches[0]

            evidence_text = evidence.get("text")
            if isinstance(evidence_text, str) and evidence_text.strip():
                text_matches = [
                    segment.get("id")
                    for segment in segments
                    if evidence_text.strip()
                    in {
                        str(segment.get("text") or "").strip(),
                        str(segment.get("raw_text") or "").strip(),
                        str(segment.get("corrected_text") or "").strip(),
                    }
                ]
                if len(text_matches) == 1:
                    return text_matches[0]
            return None

        def clean(value: Any) -> Any:
            if isinstance(value, list):
                return [
                    cleaned
                    for item in value
                    if (cleaned := clean(item)) is not discarded
                ]
            if not isinstance(value, dict):
                return copy.deepcopy(value)
            if "status" in value:
                status = value.get("status")
                if status in {"confirmed", "needs_confirmation"}:
                    raw_value = value.get("raw_value")
                    normalized = (
                        raw_value.strip()
                        if isinstance(raw_value, str)
                        else ""
                    )
                    source_segment_id = segment_id_for(value)
                    segment_position = segment_positions.get(source_segment_id)
                    source_text = (
                        str(segments[segment_position].get("text") or "")
                        if segment_position is not None
                        else ""
                    )
                    if normalized in generic_values:
                        warnings.append(
                            f"generic clinical value '{normalized}' was discarded"
                        )
                        return discarded
                    if segment_position is None:
                        warnings.append(
                            "clinical value without unique source evidence was discarded"
                        )
                        return discarded
                    if "?" in source_text:
                        warnings.append(
                            "clinical value supported only by a question was discarded"
                        )
                        return discarded
                    cleaned_value = copy.deepcopy(value)
                    if set(value.get("evidence") or {}) != {"source_segment_id"}:
                        warnings.append(
                            "clinical evidence was normalized to source_segment_id"
                        )
                    cleaned_value["evidence"] = {
                        "source_segment_id": source_segment_id
                    }
                    return cleaned_value
                return copy.deepcopy(value)
            cleaned_dict: dict[str, Any] = {}
            for key, item in value.items():
                cleaned = clean(item)
                if cleaned is not discarded:
                    cleaned_dict[key] = cleaned
            return cleaned_dict

        sanitized = clean(model_record)
        if not isinstance(sanitized, dict):
            sanitized = {}

        def has_context(value: Any, terms: tuple[str, ...]) -> bool:
            if not isinstance(value, dict) or value.get("status") not in {
                "confirmed",
                "needs_confirmation",
            }:
                return True
            position = segment_positions.get(segment_id_for(value))
            if position is None:
                return False
            start = max(0, position - 1)
            context = " ".join(
                str(segment.get("text") or "")
                for segment in segments[start : position + 1]
            )
            return any(term in context for term in terms)

        social = sanitized.get("social_history")
        if isinstance(social, dict):
            for key, terms in {
                "smoking": ("흡연", "담배", "피우"),
                "alcohol": ("음주", "술", "소주", "맥주"),
            }.items():
                if key in social and not has_context(social[key], terms):
                    social.pop(key)
                    warnings.append(f"wrong-topic social_history.{key} was discarded")

        allergy = sanitized.get("drug_allergy")
        if allergy is not None and not has_context(
            allergy, ("알레르", "알러지", "과민")
        ):
            sanitized.pop("drug_allergy", None)
            warnings.append("wrong-topic drug_allergy was discarded")

        return sanitized, warnings

    def _extract_clinical_record_stage(
        self,
        whisper_payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.prompt_path.is_file():
            raise ValueError(f"ClinicalNLP API2 prompt is missing: {self.prompt_path}")
        task = self.prompt_path.read_text(encoding="utf-8")
        selected = select_clinical_record_candidate(
            self._generate_chunk(
                task,
                clinical_payload := self._model_payload(whisper_payload),
                response_format=self._response_format(clinical_payload),
                required_key="clinical_record",
                required_type=dict,
                output_label="clinical record",
            )
        )
        sanitized_record, guard_warnings = self._sanitize_model_record(
            selected["clinical_record"], whisper_payload
        )
        selected = dict(selected)
        selected["clinical_record"] = sanitized_record
        merged = {
            "clinical_record": selected["clinical_record"],
            "unresolved_questions": selected.get("unresolved_questions", []),
        }
        result = extract_clinical_record(
            whisper_payload,
            merged,
            model_name=self.model_name,
            prompt_version=CLINICAL_PROMPT_VERSION,
        )
        supplemental, warnings = self._supplemental_fields(
            selected["clinical_record"], whisper_payload
        )
        result["clinical_record"].update(supplemental)
        result["validation_warnings"].extend(guard_warnings)
        result["validation_warnings"].extend(warnings)
        return result

    def extract_record(self, whisper_payload: dict[str, Any]) -> dict[str, Any]:
        """Generate only the conversation-grounded clinical record stage."""

        stage_errors: list[dict[str, str]] = []
        try:
            result = self._extract_clinical_record_stage(whisper_payload)
        except Exception as error:
            stage_errors.append(
                {
                    "stage": "clinical_record_extraction",
                    "code": type(error).__name__,
                    "detail": str(error),
                }
            )
            result = {
                "schema_version": "clinical-record-v2",
                "clinical_record": {},
                "unresolved_questions": [],
                "validation_warnings": [],
                "metadata": {
                    "model": self.model_name,
                    "prompt_version": CLINICAL_PROMPT_VERSION,
                },
            }
        result["stage_errors"] = stage_errors
        return result

    def finalize_record(
        self,
        result: dict[str, Any],
        whisper_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Adjudicate candidates after retrieval without regenerating the record."""

        if not isinstance(result, dict):
            raise ValueError("clinical record stage returned an invalid contract")
        stage_errors = result.get("stage_errors")
        if not isinstance(stage_errors, list):
            stage_errors = []
            result["stage_errors"] = stage_errors

        candidate_payload = self._candidate_payload(whisper_payload)
        (
            candidate_adjudication_payload,
            candidate_decisions,
        ) = self._candidate_adjudication_payload(candidate_payload)
        if self._has_candidate_annotations(candidate_adjudication_payload):
            try:
                if not self.candidate_prompt_path.is_file():
                    raise ValueError(
                        "ClinicalNLP candidate prompt is missing: "
                        f"{self.candidate_prompt_path}"
                    )
                candidate_task = self.candidate_prompt_path.read_text(
                    encoding="utf-8"
                )
                candidate_output = self._generate_chunk(
                    candidate_task,
                    candidate_adjudication_payload,
                    response_format=candidate_adjudication_response_format(),
                    required_key="candidate_decisions",
                    required_type=list,
                    output_label="candidate adjudication",
                )[-1]
                model_candidate_decisions = [
                    decision
                    for decision in candidate_output.get("candidate_decisions", [])
                    if isinstance(decision, dict)
                ]
                candidate_decisions = (
                    model_candidate_decisions + candidate_decisions
                )
            except Exception as error:
                stage_errors.append(
                    {
                        "stage": "candidate_adjudication",
                        "code": type(error).__name__,
                        "detail": str(error),
                    }
                )
        result["candidate_decisions"] = candidate_decisions
        result["metadata"]["candidate_prompt_version"] = (
            CANDIDATE_PROMPT_VERSION
            if self._has_candidate_annotations(candidate_adjudication_payload)
            else None
        )
        direct_suggestions, normalization_payload = build_draft_normalization_plan(
            result.get("clinical_record") or {},
            candidate_payload.get("segments") or [],
        )
        draft_suggestions = list(direct_suggestions)
        normalization_fields = normalization_payload.get("fields") or []
        if normalization_fields:
            try:
                if not self.draft_normalization_prompt_path.is_file():
                    raise ValueError(
                        "ClinicalNLP draft normalization prompt is missing: "
                        f"{self.draft_normalization_prompt_path}"
                    )
                normalization_task = self.draft_normalization_prompt_path.read_text(
                    encoding="utf-8"
                )
                normalization_output = self._generate_chunk(
                    normalization_task,
                    normalization_payload,
                    response_format=draft_normalization_response_format(
                        (field["field_id"] for field in normalization_fields),
                        (field["atom_id"] for field in normalization_fields),
                        (
                            candidate["candidate_id"]
                            for field in normalization_fields
                            for candidate in field.get("allowed_candidates", [])
                        ),
                    ),
                    required_key="draft_suggestions",
                    required_type=list,
                    output_label="draft normalization",
                )[-1]
                draft_suggestions.extend(
                    ground_model_draft_suggestions(
                        normalization_output,
                        normalization_payload,
                    )
                )
            except Exception as error:
                result.setdefault("validation_warnings", []).append(
                    "draft normalization unavailable; original clinical values "
                    f"were preserved ({type(error).__name__})"
                )
        result["draft_suggestions"] = draft_suggestions
        result["metadata"]["draft_normalization_prompt_version"] = (
            DRAFT_NORMALIZATION_PROMPT_VERSION
            if normalization_fields
            else None
        )
        result["stage_errors"] = stage_errors
        return result

    def extract(self, whisper_payload: dict[str, Any]) -> dict[str, Any]:
        return self.finalize_record(
            self.extract_record(whisper_payload),
            whisper_payload,
        )

