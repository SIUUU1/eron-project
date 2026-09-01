from __future__ import annotations

import threading
import unittest

from clinicalnlp_api3.clinical_llm import ClinicalLlmClient
from clinicalnlp_api3.medical_query_resolver import (
    CandidateEvidence,
    LocalDictionaryMatch,
    QueryResolution,
    QueryTextSpan,
    ResolvedCandidate,
    UmlsCandidateProvenance,
)
from clinicalnlp_api3.record_extractor import LlamaServerClinicalExtractor
from clinicalnlp_api3.workflow import run_clinical_workflow


class _EmptyRetriever:
    def retrieve(self, *, raw_text, context):
        del raw_text, context
        return []


class _ConcurrentExpander:
    def __init__(self, extraction_started: threading.Event) -> None:
        self.expansion_started = threading.Event()
        self.extraction_started = extraction_started

    def expand(self, segments, *, covered_spans):
        del covered_spans
        self.expansion_started.set()
        if not self.extraction_started.wait(1.0):
            raise AssertionError("clinical extraction did not run concurrently")
        return {
            "status": "available",
            "fallback_used": False,
            "method": "SYNTHETIC_TRANSLATION",
            "translated_segments": [
                {
                    "segment_id": segment["id"],
                    "translated_text_en": "The patient takes amlodipine.",
                }
                for segment in segments
            ],
            "items": [],
            "partial": False,
            "failed_segment_ids": [],
        }


class _StagedExtractor:
    def __init__(self, expansion_started: threading.Event) -> None:
        self.expansion_started = expansion_started
        self.extraction_started = threading.Event()
        self.finalize_calls = 0

    def extract_record(self, payload):
        del payload
        self.extraction_started.set()
        if not self.expansion_started.wait(1.0):
            raise AssertionError("translation did not run concurrently")
        return {
            "schema_version": "clinical-record-v2",
            "clinical_record": {
                "medications": [{
                    "raw_value": "암로디핀 복용 중",
                    "status": "confirmed",
                    "evidence": {"source_segment_id": "seg_0001"},
                }]
            },
            "unresolved_questions": [],
            "candidate_decisions": [],
            "draft_suggestions": [],
            "validation_warnings": [],
            "metadata": {
                "model": "synthetic-model",
                "prompt_version": "synthetic-prompt-v1",
                "candidate_prompt_version": None,
                "draft_normalization_prompt_version": None,
            },
            "stage_errors": [],
        }

    def finalize_record(self, extracted, payload):
        del payload
        self.finalize_calls += 1
        return extracted


class _RecordingResolver:
    mode = "umls_primary"

    def __init__(self) -> None:
        self.documents = []

    def resolve(self, document):
        self.documents.append(document)
        return QueryResolution(
            mode="umls_primary",
            status="complete",
            policy_version="synthetic-policy-v1",
        )


class FieldRoutingPrimaryWorkflowTests(unittest.TestCase):
    def test_multiple_explicit_chief_complaints_are_separated_with_commas(self):
        cases = (
            ("seg_0001", "배가 아파요."),
            ("seg_0002", "설사도 했어요."),
            ("seg_0003", "피도 나요."),
        )

        class MultiChiefLlm(ClinicalLlmClient):
            def generate_json(
                self,
                *,
                system_prompt,
                user_payload,
                response_format,
                output_label,
            ):
                del system_prompt, user_payload, response_format
                if output_label != "clinical record":
                    raise AssertionError(f"unexpected LLM task: {output_label}")
                return {
                    "clinical_record": {
                        "chief_complaint": [
                            {
                                "raw_value": raw_text,
                                "status": "confirmed",
                                "evidence": {"source_segment_id": segment_id},
                            }
                            for segment_id, raw_text in cases
                        ]
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": segment_id,
                        "start": float(index),
                        "end": float(index + 1),
                        "text": raw_text,
                    }
                    for index, (segment_id, raw_text) in enumerate(cases)
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local",
                llm_client=MultiChiefLlm(),
            ),
            medical_query_resolver=_RecordingResolver(),
        )

        self.assertEqual(
            result["draft"]["fields"]["chief"]["value"],
            "배가 아파요., 설사도 했어요., 피도 나요.",
        )
        self.assertEqual(
            [
                item["raw_value"]
                for item in result["api2"]["clinical_record"]["chief_complaint"]
            ],
            [raw_text for _, raw_text in cases],
        )

    def test_chief_complaint_prefers_contextual_term_and_keeps_canonical_candidate(self):
        cases = (
            (
                "seg_0001",
                "배가 아파요.",
                "I have abdominal pain.",
                "abdominal pain",
                "Abdominal pain",
                "C0000737",
            ),
            (
                "seg_0002",
                "설사도 했어요.",
                "I also had diarrhea.",
                "diarrhea",
                None,
                None,
            ),
            (
                "seg_0003",
                "피도 나요.",
                "There is blood in the stool.",
                "blood in the stool",
                "Hematochezia",
                "C0018932",
            ),
        )

        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": case[2],
                        }
                        for segment, case in zip(segments, cases, strict=True)
                    ],
                    "items": [
                        {
                            "segment_id": segment["id"],
                            "source_span": {
                                "text": case[1].rstrip("."),
                                "start_char": 0,
                                "end_char": len(case[1].rstrip(".")),
                            },
                            "search_terms_en": [case[3]],
                            "term_type": "symptom_or_sign",
                            "expansion_method": "GEMMA_FULL_SEGMENT_TRANSLATION",
                        }
                        for segment, case in zip(segments, cases, strict=True)
                    ],
                }

        class Resolver:
            mode = "umls_primary"

            def resolve(self, document):
                candidates = []
                for query_segment, case in zip(document.segments, cases, strict=True):
                    canonical_en = case[4]
                    if canonical_en is None:
                        continue
                    translated_term = case[3]
                    start = case[2].casefold().index(translated_term.casefold())
                    candidates.append(ResolvedCandidate(
                        segment_id=query_segment.segment_id,
                        route="umls",
                        review_status="needs_review",
                        dictionary_match=LocalDictionaryMatch(
                            collection="emergency_terms",
                            entity_id=f"emergency:{case[5]}",
                            dictionary_version="medical-dictionary-v1",
                            canonical_ko=canonical_en,
                            canonical_en=canonical_en,
                            retrieval_score=0.96,
                        ),
                        evidence=CandidateEvidence(
                            scope="whole_raw_segment",
                            translated_query_span=QueryTextSpan(
                                text=translated_term,
                                start_char=start,
                                end_char=start + len(translated_term),
                            ),
                        ),
                        umls_provenance=UmlsCandidateProvenance(
                            cui=case[5],
                            semantic_types=("T184",),
                            linking_score=0.97,
                        ),
                    ))
                return QueryResolution(
                    mode="umls_primary",
                    status="complete",
                    policy_version="synthetic-policy-v1",
                    candidates=tuple(candidates),
                )

        class MultiChiefLlm(ClinicalLlmClient):
            def generate_json(
                self,
                *,
                system_prompt,
                user_payload,
                response_format,
                output_label,
            ):
                del system_prompt, user_payload, response_format
                if output_label == "clinical record":
                    return {
                        "clinical_record": {
                            "chief_complaint": [
                                {
                                    "raw_value": case[1],
                                    "status": "confirmed",
                                    "evidence": {
                                        "source_segment_id": case[0]
                                    },
                                }
                                for case in cases
                            ]
                        },
                        "unresolved_questions": [],
                    }
                if output_label == "draft normalization":
                    return {"draft_suggestions": []}
                raise AssertionError(f"unexpected LLM task: {output_label}")

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": case[0],
                        "start": float(index),
                        "end": float(index + 1),
                        "text": case[1],
                    }
                    for index, case in enumerate(cases)
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local",
                llm_client=MultiChiefLlm(),
            ),
            query_expander=Expander(),
            medical_query_resolver=Resolver(),
        )

        self.assertEqual(
            result["draft"]["fields"]["chief"]["value"],
            "Abdominal pain, Diarrhea, Blood in the stool",
        )
        self.assertEqual(
            result["api3"]["segments"][2]["annotations"][0]["candidates"][0][
                "canonical_en"
            ],
            "Hematochezia",
        )
        self.assertEqual(
            [item["segment_id"] for item in result["draft"]["fields"]["chief"]["evidence"]],
            [case[0] for case in cases],
        )

    def test_chief_complaint_uses_umls_semantic_types_without_expansion_items(self):
        raw_text = "4일 전부터 코프, 스푸텀, 디스프니아가 증가했습니다."
        translation = (
            "A 71-year-old male has had increased cough, sputum, and dyspnea "
            "since 4 days ago."
        )
        terms = (
            ("cough", "기침", "Cough", "C0010200"),
            ("sputum", "객담", "Sputum production", "C0038056"),
            ("dyspnea", "호흡곤란", "Dyspnea", "C0013404"),
        )

        class Expander:
            def expand(self, segments, *, covered_spans):
                del segments, covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [{
                        "segment_id": "seg_0001",
                        "translated_text_en": translation,
                    }],
                    "items": [],
                }

        class Extractor:
            def extract_record(self, payload):
                del payload
                return {
                    "schema_version": "clinical-record-v2",
                    "clinical_record": {
                        "chief_complaint": [{
                            "raw_value": raw_text,
                            "status": "confirmed",
                            "evidence": {"source_segment_id": "seg_0001"},
                        }],
                    },
                    "unresolved_questions": [],
                    "candidate_decisions": [],
                    "draft_suggestions": [],
                    "validation_warnings": [],
                    "metadata": {
                        "model": "synthetic-model",
                        "prompt_version": "synthetic-prompt-v1",
                    },
                    "stage_errors": [],
                }

            def finalize_record(self, extracted, payload):
                del payload
                return extracted

        class Resolver:
            mode = "umls_primary"

            def resolve(self, document):
                segment_id = document.segments[0].segment_id
                return QueryResolution(
                    mode="umls_primary",
                    status="complete",
                    policy_version="synthetic-policy-v1",
                    candidates=tuple(
                        ResolvedCandidate(
                            segment_id=segment_id,
                            route="umls",
                            review_status="needs_review",
                            dictionary_match=LocalDictionaryMatch(
                                collection="emergency_terms",
                                entity_id=f"emergency:{cui}",
                                dictionary_version="medical-dictionary-v1",
                                canonical_ko=canonical_ko,
                                canonical_en=canonical_en,
                                retrieval_score=0.96,
                            ),
                            evidence=CandidateEvidence(
                                scope="whole_raw_segment",
                                translated_query_span=QueryTextSpan(
                                    text=term,
                                    start_char=translation.index(term),
                                    end_char=translation.index(term) + len(term),
                                ),
                            ),
                            umls_provenance=UmlsCandidateProvenance(
                                cui=cui,
                                semantic_types=("T184",),
                                linking_score=0.97,
                            ),
                        )
                        for term, canonical_ko, canonical_en, cui in terms
                    ),
                )

        result = run_clinical_workflow(
            {"segments": [{
                "id": "seg_0001",
                "start": 0.0,
                "end": 1.0,
                "text": raw_text,
            }]},
            retriever=_EmptyRetriever(),
            clinical_extractor=Extractor(),
            query_expander=Expander(),
            medical_query_resolver=Resolver(),
        )

        self.assertEqual(
            result["draft"]["fields"]["chief"]["value"],
            "Cough, Sputum, Dyspnea",
        )

    def test_chief_complaint_resolves_each_gemma_medical_span_independently(self):
        raw_text = (
            "A female patient suddenly developed right eye pain, headache, and "
            "blurred vision starting 2 hours ago."
        )
        translation = (
            "A female patient suddenly developed right eye pain, headache, and "
            "blurred vision starting 2 hours ago."
        )
        terms = (
            ("right eye pain", "right eye pain", "통증", "Pain", "C0030193"),
            ("headache", "headache", "두통", "Headache", "C0018681"),
            (
                "blurred vision",
                "blurred vision",
                "시야 흐림",
                "Blurred vision",
                "C0344232",
            ),
        )

        class Expander:
            def expand(self, segments, *, covered_spans):
                del segments, covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [{
                        "segment_id": "seg_0001",
                        "translated_text_en": translation,
                    }],
                    "items": [
                        {
                            "segment_id": "seg_0001",
                            "source_span": {
                                "text": source_text,
                                "start_char": raw_text.index(source_text),
                                "end_char": raw_text.index(source_text) + len(source_text),
                            },
                            "search_terms_en": [search_term],
                            "term_type": "symptom_or_sign",
                            "expansion_method": "GEMMA_FULL_SEGMENT_TRANSLATION",
                        }
                        for source_text, search_term, *_ in terms
                    ],
                }

        class Extractor:
            def extract_record(self, payload):
                del payload
                return {
                    "schema_version": "clinical-record-v2",
                    "clinical_record": {
                        "chief_complaint": [{
                            "raw_value": raw_text,
                            "status": "confirmed",
                            "evidence": {"source_segment_id": "seg_0001"},
                        }],
                    },
                    "unresolved_questions": [],
                    "candidate_decisions": [],
                    "draft_suggestions": [],
                    "validation_warnings": [],
                    "metadata": {
                        "model": "synthetic-model",
                        "prompt_version": "synthetic-prompt-v1",
                    },
                    "stage_errors": [],
                }

            def finalize_record(self, extracted, payload):
                del payload
                return extracted

        class Resolver:
            mode = "umls_primary"

            def resolve(self, document):
                self.queries = [
                    segment.translated_text_en for segment in document.segments
                ]
                self.asserted_queries = [item[1] for item in terms]
                if self.queries != self.asserted_queries:
                    raise AssertionError(
                        f"expected independent medical queries, got {self.queries!r}"
                    )
                candidates = []
                for segment, term in zip(document.segments, terms, strict=True):
                    _, search_term, canonical_ko, canonical_en, cui = term
                    candidates.append(ResolvedCandidate(
                        segment_id=segment.segment_id,
                        route="umls",
                        review_status="needs_review",
                        dictionary_match=LocalDictionaryMatch(
                            collection="emergency_terms",
                            entity_id=f"emergency:{cui}",
                            dictionary_version="medical-dictionary-v1",
                            canonical_ko=canonical_ko,
                            canonical_en=canonical_en,
                            retrieval_score=0.96,
                        ),
                        evidence=CandidateEvidence(
                            scope="whole_raw_segment",
                            translated_query_span=QueryTextSpan(
                                text=search_term,
                                start_char=0,
                                end_char=len(search_term),
                            ),
                        ),
                        umls_provenance=UmlsCandidateProvenance(
                            cui=cui,
                            semantic_types=("T184",),
                            linking_score=0.97,
                        ),
                    ))
                return QueryResolution(
                    mode="umls_primary",
                    status="complete",
                    policy_version="synthetic-policy-v1",
                    candidates=tuple(candidates),
                )

        result = run_clinical_workflow(
            {"segments": [{
                "id": "seg_0001",
                "start": 0.0,
                "end": 1.0,
                "text": raw_text,
            }]},
            retriever=_EmptyRetriever(),
            clinical_extractor=Extractor(),
            query_expander=Expander(),
            medical_query_resolver=Resolver(),
        )

        self.assertEqual(
            result["draft"]["fields"]["chief"]["value"],
            "Right eye pain, Headache, Blurred vision",
        )

    def test_chief_complaint_uses_full_translation_when_no_term_is_detected(self):
        raw_text = "감기가 있어요."
        translation = "I have a common cold."

        class Expander:
            def expand(self, segments, *, covered_spans):
                del segments, covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [{
                        "segment_id": "seg_0001",
                        "translated_text_en": translation,
                    }],
                    "items": [],
                }

        class Extractor:
            def extract_record(self, payload):
                del payload
                return {
                    "schema_version": "clinical-record-v2",
                    "clinical_record": {
                        "chief_complaint": [{
                            "raw_value": raw_text,
                            "status": "confirmed",
                            "evidence": {"source_segment_id": "seg_0001"},
                        }],
                    },
                    "unresolved_questions": [],
                    "candidate_decisions": [],
                    "draft_suggestions": [],
                    "validation_warnings": [],
                    "metadata": {
                        "model": "synthetic-model",
                        "prompt_version": "synthetic-prompt-v1",
                    },
                    "stage_errors": [],
                }

            def finalize_record(self, extracted, payload):
                del payload
                return extracted

        result = run_clinical_workflow(
            {"segments": [{
                "id": "seg_0001",
                "start": 0.0,
                "end": 1.0,
                "text": raw_text,
            }]},
            retriever=_EmptyRetriever(),
            clinical_extractor=Extractor(),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        self.assertEqual(
            result["draft"]["fields"]["chief"]["value"],
            translation,
        )

    def test_draft_terminology_uses_translation_aligned_umls_candidate(self):
        raw_text = "디오트로피움 복용 중입니다."
        translation = "The patient takes tiotropium."
        translated_term = "tiotropium"

        class Expander:
            def expand(self, segments, *, covered_spans):
                del segments, covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [{
                        "segment_id": "seg_0001",
                        "translated_text_en": translation,
                    }],
                    "items": [{
                        "segment_id": "seg_0001",
                        "source_span": {
                            "text": "디오트로피움",
                            "start_char": 0,
                            "end_char": len("디오트로피움"),
                        },
                        "search_terms_en": [translated_term],
                        "term_type": "drug",
                        "expansion_method": "GEMMA_FULL_SEGMENT_TRANSLATION",
                    }],
                }

        class Resolver:
            mode = "umls_primary"

            def resolve(self, document):
                start = translation.index(translated_term)
                return QueryResolution(
                    mode="umls_primary",
                    status="complete",
                    policy_version="synthetic-policy-v1",
                    candidates=(ResolvedCandidate(
                        segment_id=document.segments[0].segment_id,
                        route="umls",
                        review_status="needs_review",
                        dictionary_match=LocalDictionaryMatch(
                            collection="drug_terms",
                            entity_id="drug:ingredient:tiotropium",
                            dictionary_version="medical-dictionary-v1",
                            canonical_ko="티오트로피움",
                            canonical_en="Tiotropium",
                            retrieval_score=0.95,
                        ),
                        evidence=CandidateEvidence(
                            scope="whole_raw_segment",
                            translated_query_span=QueryTextSpan(
                                text=translated_term,
                                start_char=start,
                                end_char=start + len(translated_term),
                            ),
                        ),
                        umls_provenance=UmlsCandidateProvenance(
                            cui="C0040165",
                            semantic_types=("T121",),
                            linking_score=0.96,
                        ),
                    ),),
                )

        class TranslationAwareLlm(ClinicalLlmClient):
            def generate_json(
                self,
                *,
                system_prompt,
                user_payload,
                response_format,
                output_label,
            ):
                del system_prompt, response_format
                if output_label == "clinical record":
                    return {
                        "clinical_record": {
                            "medications": {
                                "items": [{
                                    "raw_value": raw_text,
                                    "status": "confirmed",
                                    "evidence": {
                                        "source_segment_id": "seg_0001"
                                    },
                                }]
                            }
                        },
                        "unresolved_questions": [],
                    }
                if output_label == "draft normalization":
                    field = user_payload["fields"][0]
                    if field.get("translated_text_en") != translation:
                        return {"draft_suggestions": []}
                    candidate_id = field["allowed_candidates"][0]["candidate_id"]
                    return {
                        "draft_suggestions": [{
                            "field_id": "medications",
                            "atom_id": field["atom_id"],
                            "suggested_value": "Tiotropium 복용 중입니다.",
                            "applied_candidate_ids": [candidate_id],
                        }]
                    }
                raise AssertionError(f"unexpected LLM task: {output_label}")

        result = run_clinical_workflow(
            {"segments": [{
                "id": "seg_0001",
                "start": 0.0,
                "end": 2.0,
                "text": raw_text,
            }]},
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local",
                llm_client=TranslationAwareLlm(),
            ),
            query_expander=Expander(),
            medical_query_resolver=Resolver(),
        )

        medication = result["draft"]["fields"]["medication"]
        self.assertEqual(medication["value"], "Tiotropium 복용 중임.")
        self.assertEqual(medication["status"], "needs_review")
        self.assertEqual(medication["suggestion_status"], "AUTO_SUGGESTED")
        self.assertEqual(
            result["api2"]["clinical_record"]["medications"]["items"][0][
                "raw_value"
            ],
            raw_text,
        )

    def test_ros_summary_preserves_positive_negative_and_uncertain_assertions(self):
        cases = (
            ("seg_0001", "배가 아파요.", "I have abdominal pain.", "abdominal pain", "복통", "C0000737", "confirmed"),
            ("seg_0002", "구토는 없어요.", "There is no vomiting.", "vomiting", "구토", "C0042963", "confirmed"),
            ("seg_0003", "열이 있는지 잘 모르겠어요.", "I am not sure whether I have a fever.", "fever", "발열", "C0015967", "needs_confirmation"),
        )

        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                translated_segments = []
                items = []
                for segment, case in zip(segments, cases, strict=True):
                    _, raw_text, translation, search_term, *_ = case
                    translated_segments.append({
                        "segment_id": segment["id"],
                        "translated_text_en": translation,
                    })
                    source_text = raw_text.rstrip(".")
                    items.append({
                        "segment_id": segment["id"],
                        "source_span": {
                            "text": source_text,
                            "start_char": 0,
                            "end_char": len(source_text),
                        },
                        "search_terms_en": [search_term],
                        "term_type": "symptom_or_sign",
                        "expansion_method": "GEMMA_FULL_SEGMENT_TRANSLATION",
                    })
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": translated_segments,
                    "items": items,
                }

        class Extractor:
            def extract_record(self, payload):
                del payload

                def atom(index):
                    segment_id, raw_text, *_, status = cases[index]
                    return {
                        "raw_value": raw_text,
                        "status": status,
                        "evidence": {"source_segment_id": segment_id},
                    }

                return {
                    "schema_version": "clinical-record-v2",
                    "clinical_record": {
                        "chief_complaint": atom(0),
                        "history_of_present_illness": {
                            "associated_symptoms": [atom(1)],
                        },
                        "review_of_systems": [atom(2)],
                    },
                    "unresolved_questions": [],
                    "candidate_decisions": [],
                    "draft_suggestions": [],
                    "validation_warnings": [],
                    "metadata": {
                        "model": "synthetic-model",
                        "prompt_version": "synthetic-prompt-v1",
                    },
                    "stage_errors": [],
                }

            def finalize_record(self, extracted, payload):
                del payload
                return extracted

        class Resolver:
            mode = "umls_primary"

            def resolve(self, document):
                candidates = []
                for query_segment, case in zip(document.segments, cases, strict=True):
                    _, _, translation, search_term, canonical_ko, cui, _ = case
                    start = translation.casefold().index(search_term.casefold())
                    candidates.append(ResolvedCandidate(
                        segment_id=query_segment.segment_id,
                        route="umls",
                        review_status="needs_review",
                        dictionary_match=LocalDictionaryMatch(
                            collection="emergency_terms",
                            entity_id=f"emergency:{cui}",
                            dictionary_version="medical-dictionary-v1",
                            canonical_ko=canonical_ko,
                            canonical_en=search_term.title(),
                            retrieval_score=0.96,
                        ),
                        evidence=CandidateEvidence(
                            scope="whole_raw_segment",
                            translated_query_span=QueryTextSpan(
                                text=search_term,
                                start_char=start,
                                end_char=start + len(search_term),
                            ),
                        ),
                        umls_provenance=UmlsCandidateProvenance(
                            cui=cui,
                            semantic_types=("T184",),
                            linking_score=0.97,
                        ),
                    ))
                return QueryResolution(
                    mode="umls_primary",
                    status="complete",
                    policy_version="synthetic-policy-v1",
                    candidates=tuple(candidates),
                )

        result = run_clinical_workflow(
            {"segments": [{
                "id": segment_id,
                "start": float(index),
                "end": float(index + 1),
                "text": raw_text,
            } for index, (segment_id, raw_text, *_) in enumerate(cases)]},
            retriever=_EmptyRetriever(),
            clinical_extractor=Extractor(),
            query_expander=Expander(),
            medical_query_resolver=Resolver(),
        )

        self.assertEqual(
            result["draft"]["fields"]["review-of-systems"]["value"],
            "복통(+)\n구토(-)\n발열(확인 필요)",
        )

    def test_patient_reported_chief_symptom_is_mirrored_as_positive_ros_summary(self):
        raw_text = "배가 아파요."
        translation = "I have abdominal pain."
        translated_term = "abdominal pain"

        class Expander:
            def expand(self, segments, *, covered_spans):
                del segments, covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [{
                        "segment_id": "seg_0001",
                        "translated_text_en": translation,
                    }],
                    "items": [{
                        "segment_id": "seg_0001",
                        "source_span": {
                            "text": raw_text.rstrip("."),
                            "start_char": 0,
                            "end_char": len(raw_text.rstrip(".")),
                        },
                        "search_terms_en": [translated_term],
                        "term_type": "symptom_or_sign",
                        "expansion_method": "GEMMA_FULL_SEGMENT_TRANSLATION",
                    }],
                }

        class Extractor:
            def extract_record(self, payload):
                del payload
                return {
                    "schema_version": "clinical-record-v2",
                    "clinical_record": {
                        "chief_complaint": {
                            "raw_value": raw_text,
                            "status": "confirmed",
                            "evidence": {"source_segment_id": "seg_0001"},
                        },
                        "review_of_systems": [{
                            "raw_value": raw_text,
                            "status": "confirmed",
                            "evidence": {"source_segment_id": "seg_0001"},
                        }],
                    },
                    "unresolved_questions": [],
                    "candidate_decisions": [],
                    "draft_suggestions": [],
                    "validation_warnings": [],
                    "metadata": {
                        "model": "synthetic-model",
                        "prompt_version": "synthetic-prompt-v1",
                    },
                    "stage_errors": [],
                }

            def finalize_record(self, extracted, payload):
                del payload
                return extracted

        class Resolver:
            mode = "umls_primary"

            def resolve(self, document):
                query_segment = document.segments[0]
                if query_segment.translated_text_en != translated_term:
                    raise AssertionError(
                        "chief complaint must resolve the extracted medical term"
                    )
                return QueryResolution(
                    mode="umls_primary",
                    status="complete",
                    policy_version="synthetic-policy-v1",
                    candidates=(ResolvedCandidate(
                        segment_id=query_segment.segment_id,
                        route="umls",
                        review_status="needs_review",
                        dictionary_match=LocalDictionaryMatch(
                            collection="emergency_terms",
                            entity_id="emergency:abdominal-pain",
                            dictionary_version="medical-dictionary-v1",
                            canonical_ko="복통",
                            canonical_en="Abdominal pain",
                            retrieval_score=0.96,
                        ),
                        evidence=CandidateEvidence(
                            scope="whole_raw_segment",
                            translated_query_span=QueryTextSpan(
                                text=translated_term,
                                start_char=0,
                                end_char=len(translated_term),
                            ),
                        ),
                        umls_provenance=UmlsCandidateProvenance(
                            cui="C0000737",
                            semantic_types=("T184",),
                            linking_score=0.97,
                        ),
                    ),),
                )

        result = run_clinical_workflow(
            {"segments": [{
                "id": "seg_0001",
                "start": 0.0,
                "end": 2.0,
                "text": raw_text,
            }]},
            retriever=_EmptyRetriever(),
            clinical_extractor=Extractor(),
            query_expander=Expander(),
            medical_query_resolver=Resolver(),
        )

        fields = result["draft"]["fields"]
        annotations = result["api3"]["segments"][0]["annotations"]
        self.assertTrue(annotations[0]["candidates"], annotations)
        self.assertEqual(
            annotations[0]["candidates"][0]["canonical_ko"],
            "복통",
        )
        self.assertEqual(fields["chief"]["value"], "Abdominal pain")
        self.assertEqual(fields["review-of-systems"]["value"], "복통(+)")
        self.assertEqual(fields["review-of-systems"]["status"], "needs_review")
        self.assertEqual(
            fields["review-of-systems"]["evidence"][0]["segment_id"],
            "seg_0001",
        )
        self.assertEqual(
            result["api2"]["clinical_record"]["chief_complaint"]["raw_value"],
            raw_text,
        )

    def test_draft_uses_concise_korean_clinical_note_endings(self):
        raw_text = (
            "숨이 차 보입니다. 객담이 증가했습니다. 색이 변했습니다. "
            "COPD가 있습니다. 검사를 했습니다."
        )

        class Expander:
            def expand(self, segments, *, covered_spans):
                del segments, covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [],
                    "items": [],
                }

        class Extractor:
            def extract_record(self, payload):
                del payload

                def atom(value):
                    return {
                        "raw_value": value,
                        "status": "confirmed",
                        "evidence": {"source_segment_id": "seg_0001"},
                    }

                return {
                    "schema_version": "clinical-record-v2",
                    "clinical_record": {
                        "chief_complaint": atom("숨이 차 보입니다."),
                        "history_of_present_illness": {
                            "course": atom("객담이 증가했습니다."),
                            "associated_symptoms": [atom("색이 변했습니다.")],
                        },
                        "past_history": {
                            "underlying_conditions": [atom("COPD가 있습니다.")],
                        },
                        "treatment_plan": [atom("검사를 했습니다.")],
                    },
                    "unresolved_questions": [],
                    "candidate_decisions": [],
                    "draft_suggestions": [],
                    "validation_warnings": [],
                    "metadata": {
                        "model": "synthetic-model",
                        "prompt_version": "synthetic-prompt-v1",
                    },
                    "stage_errors": [],
                }

            def finalize_record(self, extracted, payload):
                del payload
                return extracted

        result = run_clinical_workflow(
            {"segments": [{
                "id": "seg_0001",
                "start": 0.0,
                "end": 5.0,
                "text": raw_text,
            }]},
            retriever=_EmptyRetriever(),
            clinical_extractor=Extractor(),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        fields = result["draft"]["fields"]
        self.assertEqual(fields["chief"]["value"], "숨이 차 보임.")
        self.assertEqual(
            fields["history"]["value"],
            "객담이 증가함.\n색이 변했음.",
        )
        self.assertEqual(
            fields["past-history"]["value"].splitlines()[0],
            "COPD가 있음.",
        )
        self.assertEqual(fields["treatment-plan"]["value"], "검사를 했음.")
        self.assertEqual(
            result["api2"]["clinical_record"]["chief_complaint"]["raw_value"],
            "숨이 차 보입니다.",
        )

    def test_primary_resolver_runs_once_with_grounded_field_hints(self):
        extraction_started = threading.Event()
        expander = _ConcurrentExpander(extraction_started)
        extractor = _StagedExtractor(expander.expansion_started)
        extractor.extraction_started = extraction_started
        resolver = _RecordingResolver()

        result = run_clinical_workflow(
            {
                "segments": [{
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 2.0,
                    "text": "암로디핀 복용 중",
                }]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=extractor,
            query_expander=expander,
            medical_query_resolver=resolver,
        )

        self.assertEqual(len(resolver.documents), 1)
        self.assertEqual(
            resolver.documents[0].segments[0].collection_hints,
            frozenset({"drug_terms"}),
        )
        self.assertEqual(extractor.finalize_calls, 1)
        self.assertEqual(
            result["api3"]["segments"][0]["raw_text"],
            "암로디핀 복용 중",
        )
        self.assertNotIn("field_routing_ab", result)

    def test_translated_candidate_keeps_validated_raw_source_without_replacement_contract(self):
        raw_text = "디오트로피움 복용 중입니다."
        translation = "The patient takes tiotropium."
        translated_term = "tiotropium"

        class Expander:
            def expand(self, segments, *, covered_spans):
                del segments, covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [{
                        "segment_id": "seg_0001",
                        "translated_text_en": translation,
                    }],
                    "items": [{
                        "segment_id": "seg_0001",
                        "source_span": {
                            "text": "디오트로피움",
                            "start_char": 0,
                            "end_char": len("디오트로피움"),
                        },
                        "search_terms_en": [translated_term],
                        "term_type": "drug",
                        "expansion_method": "GEMMA_FULL_SEGMENT_TRANSLATION",
                    }],
                }

        class Extractor:
            def extract_record(self, payload):
                del payload
                return {
                    "schema_version": "clinical-record-v2",
                    "clinical_record": {
                        "medications": {
                            "items": [{
                                "raw_value": raw_text,
                                "status": "confirmed",
                                "evidence": {"source_segment_id": "seg_0001"},
                            }]
                        }
                    },
                    "unresolved_questions": [],
                    "candidate_decisions": [],
                    "draft_suggestions": [],
                    "validation_warnings": [],
                    "metadata": {
                        "model": "synthetic-model",
                        "prompt_version": "synthetic-prompt-v1",
                    },
                    "stage_errors": [],
                }

            def finalize_record(self, extracted, payload):
                del payload
                return extracted

        class Resolver:
            mode = "umls_primary"

            def resolve(self, document):
                self.document = document
                start = translation.index(translated_term)
                return QueryResolution(
                    mode="umls_primary",
                    status="complete",
                    policy_version="synthetic-policy-v1",
                    candidates=(ResolvedCandidate(
                        segment_id="q0001",
                        route="umls",
                        review_status="needs_review",
                        dictionary_match=LocalDictionaryMatch(
                            collection="drug_terms",
                            entity_id="drug:ingredient:tiotropium",
                            dictionary_version="medical-dictionary-v1",
                            canonical_ko="티오트로피움",
                            canonical_en="Tiotropium",
                            retrieval_score=0.95,
                        ),
                        evidence=CandidateEvidence(
                            scope="whole_raw_segment",
                            translated_query_span=QueryTextSpan(
                                text=translated_term,
                                start_char=start,
                                end_char=start + len(translated_term),
                            ),
                        ),
                        umls_provenance=UmlsCandidateProvenance(
                            cui="C0040165",
                            semantic_types=("T121",),
                            linking_score=0.96,
                        ),
                    ),),
                )

        result = run_clinical_workflow(
            {"segments": [{
                "id": "seg_0001",
                "start": 0.0,
                "end": 2.0,
                "text": raw_text,
            }]},
            retriever=_EmptyRetriever(),
            clinical_extractor=Extractor(),
            query_expander=Expander(),
            medical_query_resolver=Resolver(),
        )

        review = result["draft"]["review_items"][0]
        self.assertEqual(review["field_id"], "medication")
        self.assertEqual(review["source"], "디오트로피움")
        self.assertNotIn("replacement_text", review)
        self.assertNotIn("original_value", review)
        self.assertEqual(
            result["draft"]["fields"]["medication"]["value"],
            "디오트로피움 복용 중임.",
        )


if __name__ == "__main__":
    unittest.main()
