from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .clinical_llm import ClinicalLlmClient, OllamaCloudClinicalLlmClient
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
DEFAULT_MAX_OUTPUT_TOKENS = 3072
MAX_CANDIDATES_PER_ANNOTATION = 5
MAX_MODEL_CANDIDATES_PER_ANNOTATION = 3
MAX_MODEL_CANDIDATE_ANNOTATIONS = 16
CLINICAL_PROMPT_VERSION = "clinical-record-extraction-v2.12"
CANDIDATE_PROMPT_VERSION = "candidate-adjudication-v1"
DRAFT_NORMALIZATION_PROMPT_VERSION = "draft-normalization-v1"
COMPACT_PROMPT_VERSION = "clinical-record-compact-v3.2"


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
                    "600"
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

