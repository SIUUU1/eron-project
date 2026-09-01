from __future__ import annotations

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
from clinicalnlp_api3.workflow import build_draft, run_clinical_workflow
from clinicalnlp_api3.workflow_contract_v2 import to_clinical_workflow_v2


class _EmptyRetriever:
    def retrieve(self, *, raw_text, context):
        del raw_text, context
        return []


class _SequentialExpander:
    def __init__(self) -> None:
        self.expansion_completed = False

    def expand(self, segments, *, covered_spans):
        del covered_spans
        self.expansion_completed = True
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
    def __init__(self, expander: _SequentialExpander) -> None:
        self.expander = expander
        self.finalize_calls = 0

    def extract_record(self, payload):
        if not self.expander.expansion_completed:
            raise AssertionError("clinical extraction ran before translation")
        if (
            payload["segments"][0].get("translated_text_en")
            != "The patient takes amlodipine."
        ):
            raise AssertionError("clinical extraction did not receive translation")
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
                "Abdomen pain.",
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

    def test_chief_complaint_links_english_facts_to_korean_source_spans(self):
        raw_text = "71세 남자, 4일 전부터 코프, 스프텀, 디스프니아가 증가했습니다."
        translation = (
            "A 71-year-old male has had increased cough, sputum, and dyspnea "
            "since 4 days ago."
        )
        terms = (
            ("코프", "cough", "기침", "Cough", "C0010200"),
            ("스프텀", "sputum", "객담", "Sputum production", "C0038056"),
            ("디스프니아", "dyspnea", "호흡곤란", "Dyspnea", "C0013404"),
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
                        "chief_complaint": [
                            {
                                "raw_value": search_term,
                                "status": "confirmed",
                                "evidence": {"source_segment_id": "seg_0001"},
                            }
                            for _, search_term, *_ in terms
                        ],
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
                "end": 4.68,
                "text": raw_text,
            }]},
            retriever=_EmptyRetriever(),
            clinical_extractor=Extractor(),
            query_expander=Expander(),
            medical_query_resolver=Resolver(),
        )

        annotations = result["api3"]["segments"][0]["annotations"]
        self.assertEqual(
            [
                (
                    annotation["source_span"]["text"],
                    annotation["search_terms_en"][0],
                )
                for annotation in annotations
            ],
            [(source_text, search_term) for source_text, search_term, *_ in terms],
        )
        self.assertEqual(
            [
                atom["raw_value"]
                for atom in result["api2"]["clinical_record"]["chief_complaint"]
            ],
            [search_term for _, search_term, *_ in terms],
        )
        self.assertEqual(
            result["draft"]["fields"]["chief"]["value"],
            "Cough, Sputum, Dyspnea",
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

    def test_pain_assessment_combines_explicit_nrs_with_grounded_location(self):
        pain_text = "배가 너무 아파요."
        score_text = "7점이요."

        class Expander:
            def expand(self, segments, *, covered_spans):
                del segments, covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": "seg_0001",
                            "translated_text_en": "I have severe abdominal pain.",
                        },
                        {
                            "segment_id": "seg_0002",
                            "translated_text_en": "How many points is the pain?",
                        },
                        {
                            "segment_id": "seg_0003",
                            "translated_text_en": "It is 7 out of 10.",
                        },
                    ],
                    "items": [{
                        "segment_id": "seg_0001",
                        "source_span": {
                            "text": "배",
                            "start_char": 0,
                            "end_char": 1,
                        },
                        "search_terms_en": ["abdomen"],
                        "term_type": "anatomy",
                        "expansion_method": "GEMMA_FULL_SEGMENT_TRANSLATION",
                    }],
                }

        class Retriever:
            def retrieve(self, *, raw_text, context):
                del context
                if raw_text.casefold() != "abdomen":
                    return []
                return [{
                    "collection": "anatomy_terms",
                    "entity_id": "anatomy:abdomen",
                    "canonical_ko": "복부",
                    "canonical_en": "Abdomen",
                    "source_text": "abdomen",
                    "start_char": 0,
                    "end_char": len("abdomen"),
                    "match_type": "official_exact",
                    "review_status": "official",
                    "retrieval_score": 0.99,
                }]

        class Extractor:
            def extract_record(self, payload):
                del payload
                return {
                    "schema_version": "clinical-record-v2",
                    "clinical_record": {
                        "pain_assessment": {
                            "nrs": {
                                "raw_value": score_text,
                                "status": "confirmed",
                                "evidence": {"source_segment_id": "seg_0003"},
                            },
                            "location": {
                                "raw_value": "배",
                                "status": "confirmed",
                                "evidence": {"source_segment_id": "seg_0001"},
                            },
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

        result = run_clinical_workflow(
            {"segments": [
                {"id": "seg_0001", "start": 0.0, "end": 1.0, "text": pain_text},
                {
                    "id": "seg_0002",
                    "start": 1.0,
                    "end": 2.0,
                    "text": "몇 점 정도예요?",
                },
                {"id": "seg_0003", "start": 2.0, "end": 3.0, "text": score_text},
            ]},
            retriever=Retriever(),
            clinical_extractor=Extractor(),
            query_expander=Expander(),
        )

        self.assertEqual(
            result["draft"]["fields"]["pain"]["value"],
            "NRS 7 / Abdomen",
        )
        self.assertEqual(
            [item["segment_id"] for item in result["draft"]["fields"]["pain"]["evidence"]],
            ["seg_0003", "seg_0001"],
        )

    def test_pain_assessment_distinguishes_absent_and_missing_details(self):
        cases = (
            ("통증 관련 언급 없음", {}, ""),
            (
                "Abdomen pain.",
                {
                    "location": {
                        "raw_value": "Abdomen",
                        "status": "confirmed",
                        "evidence": {"source_segment_id": "seg_0001"},
                    }
                },
                "NRS - / Abdomen",
            ),
            (
                "통증은 7점이에요.",
                {
                    "nrs": {
                        "raw_value": "7점",
                        "status": "confirmed",
                        "evidence": {"source_segment_id": "seg_0001"},
                    }
                },
                "NRS 7 / -",
            ),
            (
                "오른쪽이 아프고 통증은 9점이에요.",
                {
                    "nrs": {
                        "raw_value": "9점",
                        "status": "confirmed",
                        "evidence": {"source_segment_id": "seg_0001"},
                    },
                    "location": {
                        "raw_value": "오른쪽",
                        "status": "confirmed",
                        "evidence": {"source_segment_id": "seg_0001"},
                    },
                },
                "NRS 9 / -",
            ),
            (
                "배가 아프고 통증은 9점이에요.",
                {
                    "nrs": {
                        "raw_value": "9점",
                        "status": "confirmed",
                        "evidence": {"source_segment_id": "seg_0001"},
                    },
                    "location": {
                        "raw_value": "배",
                        "status": "confirmed",
                        "evidence": {"source_segment_id": "seg_0001"},
                    },
                },
                "NRS 9 / -",
            ),
            (
                "아파요.",
                {
                    "presence": {
                        "raw_value": "아파요.",
                        "status": "confirmed",
                        "evidence": {"source_segment_id": "seg_0001"},
                    }
                },
                "NRS - / -",
            ),
            (
                "지금은 안 아파요.",
                {
                    "presence": {
                        "raw_value": "지금은 안 아파요.",
                        "status": "confirmed",
                        "evidence": {"source_segment_id": "seg_0001"},
                    }
                },
                "통증 없음",
            ),
            (
                "통증은 0점이에요.",
                {
                    "nrs": {
                        "raw_value": "0점",
                        "status": "confirmed",
                        "evidence": {"source_segment_id": "seg_0001"},
                    }
                },
                "NRS 0 / -",
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
                            "translated_text_en": "Pain-related statement.",
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class Extractor:
            def __init__(self, pain_assessment):
                self.pain_assessment = pain_assessment

            def extract_record(self, payload):
                del payload
                return {
                    "schema_version": "clinical-record-v2",
                    "clinical_record": {
                        **(
                            {"pain_assessment": self.pain_assessment}
                            if self.pain_assessment
                            else {}
                        )
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

        for raw_text, pain_assessment, expected in cases:
            with self.subTest(expected=expected):
                result = run_clinical_workflow(
                    {"segments": [{
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": raw_text,
                    }]},
                    retriever=_EmptyRetriever(),
                    clinical_extractor=Extractor(pain_assessment),
                    query_expander=Expander(),
                )
                self.assertEqual(
                    result["draft"]["fields"]["pain"]["value"],
                    expected,
                )

    def test_pain_extraction_uses_full_english_translation(self):
        raw_text = "디 엔알에스 이즈 에잇, 스퀴징 센세이션, 레프트 암으로 방사됨."
        translation = (
            "The NRS is 8, described as a squeezing sensation, and there is "
            "radiation to the left arm."
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

        class TranslationAwarePainLlm(ClinicalLlmClient):
            def generate_json(
                self,
                *,
                system_prompt,
                user_payload,
                response_format,
                output_label,
            ):
                del system_prompt, response_format
                if output_label != "clinical record":
                    raise AssertionError(f"unexpected LLM task: {output_label}")
                translated_text = user_payload["segments"][0].get(
                    "translated_text_en"
                )
                return {
                    "clinical_record": {
                        "pain_assessment": (
                            {
                                "nrs": {
                                    "raw_value": "8",
                                    "status": "confirmed",
                                    "evidence": {
                                        "source_segment_id": "seg_0001"
                                    },
                                }
                            }
                            if translated_text == translation
                            else {
                                "presence": {
                                    "raw_value": "스퀴징 센세이션",
                                    "status": "confirmed",
                                    "evidence": {
                                        "source_segment_id": "seg_0001"
                                    },
                                }
                            }
                        )
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [{
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 5.0,
                    "text": raw_text,
                }]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local",
                llm_client=TranslationAwarePainLlm(),
            ),
            query_expander=Expander(),
        )

        self.assertEqual(
            result["draft"]["fields"]["pain"]["value"],
            "NRS 8 / -",
            result,
        )
        self.assertEqual(result["api2"]["validation_warnings"], [])

    def test_hpi_uses_model_authored_text_and_only_grounded_oldcarts_facts(self):
        cases = (
            ("seg_0001", "언제부터 배가 아프셨어요?"),
            ("seg_0002", "어제 저녁부터요."),
            ("seg_0003", "어디가 제일 아프세요?"),
            ("seg_0004", "오른쪽 아랫배요."),
            ("seg_0005", "몇 점 정도 아프세요?"),
            ("seg_0006", "7점 정도요."),
            ("seg_0007", "토하거나 설사한 적은 있어요?"),
            ("seg_0008", "토는 두 번 했고 설사는 안 했어요."),
        )
        expected = (
            "전일 저녁부터 발생한 우하복부 통증으로 내원함. "
            "통증 강도는 NRS 7점이며 구토 2회 동반함. 설사는 부인함."
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
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class HpiLlm(ClinicalLlmClient):
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

                def atom(raw_value, segment_id, *, status="confirmed"):
                    return {
                        "raw_value": raw_value,
                        "status": status,
                        "evidence": {"source_segment_id": segment_id},
                    }

                return {
                    "clinical_record": {
                        "history_of_present_illness": {
                            "text": expected,
                            "onset": atom("어제 저녁부터요.", "seg_0002"),
                            "location": atom("오른쪽 아랫배요.", "seg_0004"),
                            "severity": atom("7점 정도요.", "seg_0006"),
                            "associated_symptoms": [
                                atom(
                                    "토는 두 번 했고 설사는 안 했어요.",
                                    "seg_0008",
                                )
                            ],
                        }
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
                        "text": text,
                    }
                    for index, (segment_id, text) in enumerate(cases)
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local",
                llm_client=HpiLlm(),
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        hpi = result["api2"]["clinical_record"]["history_of_present_illness"]
        self.assertEqual(result["draft"]["fields"]["history"]["value"], expected)
        self.assertEqual(hpi["character"]["status"], "not_mentioned")
        self.assertEqual(hpi["radiation"]["status"], "not_mentioned")
        self.assertEqual(hpi["timing"]["status"], "not_mentioned")
        self.assertEqual(
            [item["segment_id"] for item in result["draft"]["fields"]["history"]["evidence"]],
            ["seg_0002", "seg_0004", "seg_0006", "seg_0008"],
        )

    def test_hpi_preserves_conflicting_onsets_and_requires_review(self):
        expected = (
            "명치부위 통증으로 내원함. 통증 발생 시점에 대해 환자는 금일 "
            "아침부터라고 진술하였으나 보호자는 전일 밤부터라고 진술하여 "
            "정확한 발병 시점은 불확실함. 통증 강도는 NRS 6점임."
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
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class HpiLlm(ClinicalLlmClient):
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

                def atom(raw_value, segment_id, *, status="confirmed"):
                    return {
                        "raw_value": raw_value,
                        "status": status,
                        "evidence": {"source_segment_id": segment_id},
                    }

                return {
                    "clinical_record": {
                        "history_of_present_illness": {
                            "text": expected,
                            "onset": [
                                atom(
                                    "오늘 아침부터요.",
                                    "seg_0002",
                                    status="needs_confirmation",
                                ),
                                atom(
                                    "아니에요. 어제 밤부터 아프다고 했어요.",
                                    "seg_0003",
                                    status="needs_confirmation",
                                ),
                            ],
                            "location": atom("명치 쪽이요.", "seg_0005"),
                            "severity": atom("한 6점 정도요.", "seg_0007"),
                        }
                    },
                    "unresolved_questions": [],
                }

        segments = (
            ("seg_0001", "언제부터 배가 아팠어요?"),
            ("seg_0002", "오늘 아침부터요."),
            ("seg_0003", "아니에요. 어제 밤부터 아프다고 했어요."),
            ("seg_0004", "어디가 아프세요?"),
            ("seg_0005", "명치 쪽이요."),
            ("seg_0006", "통증은 몇 점 정도예요?"),
            ("seg_0007", "한 6점 정도요."),
        )
        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": segment_id,
                        "start": float(index),
                        "end": float(index + 1),
                        "text": text,
                    }
                    for index, (segment_id, text) in enumerate(segments)
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local",
                llm_client=HpiLlm(),
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["history"]
        self.assertEqual(field["value"], expected)
        self.assertEqual(field["status"], "needs_review")
        self.assertEqual(
            [item["segment_id"] for item in field["evidence"]],
            ["seg_0002", "seg_0003", "seg_0005", "seg_0007"],
        )

    def test_hpi_is_empty_when_current_illness_was_not_assessed(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": "Please wait here.",
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class EmptyHpiLlm(ClinicalLlmClient):
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
                return {"clinical_record": {}, "unresolved_questions": []}

        result = run_clinical_workflow(
            {
                "segments": [{
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "여기서 잠시 기다리세요.",
                }]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local",
                llm_client=EmptyHpiLlm(),
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["history"]
        self.assertEqual(field["value"], "")
        self.assertEqual(field["status"], "empty")
        self.assertEqual(field["evidence"], [])

    def test_past_history_uses_model_authored_compact_text_with_grounded_facts(self):
        cases = (
            ("seg_0001", "평소 앓고 있는 질환 있으세요?"),
            ("seg_0002", "고혈압하고 당뇨가 있어요."),
            ("seg_0003", "수술받으신 적은 있으세요?"),
            ("seg_0004", "5년 전에 담낭 제거 수술했어요."),
        )
        expected = "HTN(+), DM(+), Cholecystectomy(+, 5 years ago)"

        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class PastHistoryLlm(ClinicalLlmClient):
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

                def atom(raw_value, segment_id):
                    return {
                        "raw_value": raw_value,
                        "status": "confirmed",
                        "evidence": {"source_segment_id": segment_id},
                    }

                return {
                    "clinical_record": {
                        "past_history": {
                            "text": expected,
                            "underlying_conditions": [
                                atom("고혈압", "seg_0002"),
                                atom("당뇨", "seg_0002"),
                            ],
                            "surgery_history": [
                                atom(
                                    "5년 전에 담낭 제거 수술",
                                    "seg_0004",
                                )
                            ],
                            "previous_admissions": [],
                        }
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
                        "text": text,
                    }
                    for index, (segment_id, text) in enumerate(cases)
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local",
                llm_client=PastHistoryLlm(),
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["past-history"]
        self.assertEqual(field["value"], expected)
        self.assertEqual(field["status"], "filled")
        self.assertEqual(
            [item["segment_id"] for item in field["evidence"]],
            ["seg_0002", "seg_0004"],
        )
        self.assertEqual(
            result["api2"]["clinical_record"]["past_history"][
                "previous_admissions"
            ],
            [],
        )

    def test_past_history_allows_none_only_when_all_categories_are_denied(self):
        cases = (
            ("seg_0001", "평소 앓고 있는 질환 있으세요?"),
            ("seg_0002", "없어요."),
            ("seg_0003", "수술받거나 크게 입원하신 적은요?"),
            ("seg_0004", "그런 적도 없어요."),
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
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class NoPastHistoryLlm(ClinicalLlmClient):
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

                def denied(raw_value, segment_id):
                    return {
                        "raw_value": raw_value,
                        "status": "confirmed",
                        "evidence": {"source_segment_id": segment_id},
                    }

                return {
                    "clinical_record": {
                        "past_history": {
                            "text": "특이 과거력 없음",
                            "medical_history_status": denied(
                                "없어요.", "seg_0002"
                            ),
                            "surgical_history_status": denied(
                                "그런 적도 없어요.", "seg_0004"
                            ),
                            "admission_history_status": denied(
                                "그런 적도 없어요.", "seg_0004"
                            ),
                        }
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
                        "text": text,
                    }
                    for index, (segment_id, text) in enumerate(cases)
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local",
                llm_client=NoPastHistoryLlm(),
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["past-history"]
        self.assertEqual(field["value"], "특이 과거력 없음")
        self.assertEqual(field["status"], "filled")
        self.assertEqual(
            [item["segment_id"] for item in field["evidence"]],
            ["seg_0002", "seg_0004"],
        )

    def test_past_history_partial_denial_cannot_be_finalized_as_no_history(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class PartialDenialLlm(ClinicalLlmClient):
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
                        "past_history": {
                            "text": "특이 과거력 없음",
                            "medical_history_status": {
                                "raw_value": "없어요.",
                                "status": "confirmed",
                                "evidence": {
                                    "source_segment_id": "seg_0002"
                                },
                            },
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "평소 앓고 있는 질환 있으세요?",
                    },
                    {
                        "id": "seg_0002",
                        "start": 1.0,
                        "end": 2.0,
                        "text": "없어요.",
                    },
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local",
                llm_client=PartialDenialLlm(),
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["past-history"]
        self.assertEqual(field["value"], "특이 과거력 없음")
        self.assertEqual(field["status"], "needs_review")
        self.assertEqual(field["evidence"][0]["segment_id"], "seg_0002")

    def test_medications_keep_every_current_drug_in_model_authored_text(self):
        cases = (
            ("seg_0001", "평소 드시는 약 있으세요?"),
            ("seg_0002", "혈압약 암로디핀하고 아스피린 먹어요."),
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
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class MedicationLlm(ClinicalLlmClient):
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

                def atom(raw_value):
                    return {
                        "raw_value": raw_value,
                        "status": "confirmed",
                        "evidence": {"source_segment_id": "seg_0002"},
                    }

                return {
                    "clinical_record": {
                        "medications": {
                            "text": "Amlodipine, Aspirin",
                            "items": [atom("암로디핀"), atom("아스피린")],
                        }
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
                        "text": text,
                    }
                    for index, (segment_id, text) in enumerate(cases)
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local",
                llm_client=MedicationLlm(),
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["medication"]
        self.assertEqual(field["value"], "Amlodipine, Aspirin")
        self.assertEqual(field["status"], "filled")
        self.assertEqual(
            [item["segment_id"] for item in field["evidence"]],
            ["seg_0002"],
        )
        self.assertEqual(
            [
                item["raw_value"]
                for item in result["api2"]["clinical_record"]["medications"][
                    "items"
                ]
            ],
            ["암로디핀", "아스피린"],
        )

    def test_targeted_drug_denial_cannot_become_no_current_medication(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class TargetedDenialLlm(ClinicalLlmClient):
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
                        "medications": {
                            "text": "복용약 없음",
                            "medication_status": {
                                "raw_value": "안 먹어요.",
                                "status": "confirmed",
                                "evidence": {
                                    "source_segment_id": "seg_0002"
                                },
                            },
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "아스피린 드세요?",
                    },
                    {
                        "id": "seg_0002",
                        "start": 1.0,
                        "end": 2.0,
                        "text": "안 먹어요.",
                    },
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local",
                llm_client=TargetedDenialLlm(),
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["medication"]
        self.assertEqual(field["value"], "복용약 없음")
        self.assertEqual(field["status"], "needs_review")
        self.assertEqual(field["evidence"][0]["segment_id"], "seg_0002")

    def test_medication_display_requires_every_grounded_current_drug(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class OmissionLlm(ClinicalLlmClient):
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

                def atom(raw_value):
                    return {
                        "raw_value": raw_value,
                        "status": "confirmed",
                        "evidence": {"source_segment_id": "seg_0002"},
                    }

                return {
                    "clinical_record": {
                        "medications": {
                            "text": "Aspirin",
                            "items": [atom("암로디핀"), atom("아스피린")],
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "평소 드시는 약 있으세요?",
                    },
                    {
                        "id": "seg_0002",
                        "start": 1.0,
                        "end": 2.0,
                        "text": "혈압약 암로디핀하고 아스피린 먹어요.",
                    },
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local",
                llm_client=OmissionLlm(),
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["medication"]
        self.assertEqual(field["value"], "Aspirin")
        self.assertEqual(field["status"], "needs_review")

    def test_allergy_keeps_positive_allergen_and_reaction_separate_from_specific_denial(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class AllergyLlm(ClinicalLlmClient):
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

                def atom(raw_value):
                    return {
                        "raw_value": raw_value,
                        "status": "confirmed",
                        "evidence": {"source_segment_id": "seg_0002"},
                    }

                return {
                    "clinical_record": {
                        "drug_allergy": {
                            "text": "Penicillin - urticaria",
                            "items": [
                                {
                                    "allergy_type": "Drug",
                                    "allergen": atom("페니실린"),
                                    "reaction": atom("두드러기가 나요"),
                                }
                            ],
                            "specific_denials": [atom("조영제는 괜찮았어요")],
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "약이나 음식 알레르기 있으세요?",
                    },
                    {
                        "id": "seg_0002",
                        "start": 1.0,
                        "end": 2.0,
                        "text": "페니실린 맞으면 두드러기가 나요. 조영제는 괜찮았어요.",
                    },
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local",
                llm_client=AllergyLlm(),
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["allergy"]
        self.assertEqual(field["value"], "Penicillin - urticaria")
        self.assertEqual(field["status"], "filled")
        self.assertEqual(
            [item["segment_id"] for item in field["evidence"]],
            ["seg_0002"],
        )
        allergy = result["api2"]["clinical_record"]["drug_allergy"]
        self.assertEqual(allergy["items"][0]["allergy_type"], "Drug")
        self.assertEqual(
            allergy["specific_denials"][0]["raw_value"],
            "조영제는 괜찮았어요",
        )

    def test_allergy_allows_none_only_after_broad_explicit_denial(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class NoAllergyLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {
                    "clinical_record": {
                        "drug_allergy": {
                            "text": "알레르기 없음",
                            "allergy_status": {
                                "raw_value": "없어요",
                                "status": "confirmed",
                                "evidence": {"source_segment_id": "seg_0002"},
                            },
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "약이나 음식 알레르기 있으세요?",
                    },
                    {
                        "id": "seg_0002",
                        "start": 1.0,
                        "end": 2.0,
                        "text": "없어요.",
                    },
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=NoAllergyLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        self.assertEqual(
            result["draft"]["fields"]["allergy"]["status"], "filled"
        )

    def test_targeted_allergen_denial_cannot_become_whole_allergy_none(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class TargetedDenialLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {
                    "clinical_record": {
                        "drug_allergy": {
                            "text": "알레르기 없음",
                            "allergy_status": {
                                "raw_value": "없어요",
                                "status": "confirmed",
                                "evidence": {"source_segment_id": "seg_0002"},
                            },
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "페니실린 알레르기 있으세요?",
                    },
                    {
                        "id": "seg_0002",
                        "start": 1.0,
                        "end": 2.0,
                        "text": "없어요.",
                    },
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=TargetedDenialLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["allergy"]
        self.assertEqual(field["value"], "알레르기 없음")
        self.assertEqual(field["status"], "needs_review")

    def test_uncertain_adverse_effect_is_not_auto_confirmed_as_allergy(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class UncertainReactionLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )

                def atom(raw_value):
                    return {
                        "raw_value": raw_value,
                        "status": "needs_confirmation",
                        "evidence": {"source_segment_id": "seg_0002"},
                    }

                return {
                    "clinical_record": {
                        "drug_allergy": {
                            "text": "Unidentified medication - dyspepsia",
                            "items": [
                                {
                                    "allergy_type": "Drug",
                                    "allergen": atom("그 약"),
                                    "reaction": atom("속이 쓰린데 알레르기인지는 모르겠어요"),
                                }
                            ],
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "그 약에 알레르기가 있었나요?",
                    },
                    {
                        "id": "seg_0002",
                        "start": 1.0,
                        "end": 2.0,
                        "text": "그 약 먹으면 속이 쓰린데 알레르기인지는 모르겠어요.",
                    },
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=UncertainReactionLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["allergy"]
        self.assertEqual(field["value"], "Unidentified medication - dyspepsia")
        self.assertEqual(field["status"], "needs_review")

    def test_social_history_keeps_independent_smoking_and_alcohol_details(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class SocialHistoryLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )

                def atom(raw_value, segment_id, *, value=None):
                    result = {
                        "raw_value": raw_value,
                        "status": "confirmed",
                        "evidence": {"source_segment_id": segment_id},
                    }
                    if value is not None:
                        result["value"] = value
                    return result

                return {
                    "clinical_record": {
                        "social_history": {
                            "text": (
                                "Smoking: Current, 5 PY\n"
                                "Alcohol: 주 2회, 소주 0.5병"
                            ),
                            "smoking": {
                                "text": "Smoking: Current, 5 PY",
                                "state": "Current smoker",
                                "smoking_status": atom(
                                    "하루 반 갑 정도요. 10년 정도 피웠어요.",
                                    "seg_0002",
                                ),
                                "packs_per_day": atom(
                                    "하루 반 갑", "seg_0002", value=0.5
                                ),
                                "duration_years": atom(
                                    "10년", "seg_0002", value=10
                                ),
                            },
                            "alcohol": {
                                "text": "Alcohol: 주 2회, 소주 0.5병",
                                "alcohol_status": atom(
                                    "일주일에 두 번 정도 소주 반 병씩 마셔요.",
                                    "seg_0004",
                                ),
                                "frequency": atom(
                                    "일주일에 두 번", "seg_0004"
                                ),
                                "type": atom("소주", "seg_0004"),
                                "amount_per_occasion": atom(
                                    "반 병", "seg_0004", value=0.5
                                ),
                            },
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "담배 피우세요?",
                    },
                    {
                        "id": "seg_0002",
                        "start": 1.0,
                        "end": 2.0,
                        "text": "하루 반 갑 정도요. 10년 정도 피웠어요.",
                    },
                    {
                        "id": "seg_0003",
                        "start": 2.0,
                        "end": 3.0,
                        "text": "술은요?",
                    },
                    {
                        "id": "seg_0004",
                        "start": 3.0,
                        "end": 4.0,
                        "text": "일주일에 두 번 정도 소주 반 병씩 마셔요.",
                    },
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=SocialHistoryLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["social"]
        self.assertEqual(
            field["value"],
            "Smoking: Current, 5 PY\nAlcohol: 주 2회, 소주 0.5병",
        )
        self.assertEqual(field["status"], "filled")
        self.assertEqual(
            [item["segment_id"] for item in field["evidence"]],
            ["seg_0002", "seg_0004"],
        )
        smoking = result["api2"]["clinical_record"]["social_history"][
            "smoking"
        ]
        self.assertEqual(smoking["pack_years"], 5)
        self.assertEqual(
            smoking["pack_years_provenance"]["formula"],
            "packs_per_day * duration_years",
        )

    def test_social_history_does_not_turn_current_smoking_denial_into_never_smoker(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class CurrentDenialLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {
                    "clinical_record": {
                        "social_history": {
                            "text": "Smoking: 현재 비흡연",
                            "smoking": {
                                "text": "Smoking: 현재 비흡연",
                                "smoking_status": {
                                    "raw_value": "안 피워요",
                                    "status": "confirmed",
                                    "evidence": {
                                        "source_segment_id": "seg_0002"
                                    },
                                },
                            },
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "담배 피우세요?",
                    },
                    {
                        "id": "seg_0002",
                        "start": 1.0,
                        "end": 2.0,
                        "text": "안 피워요.",
                    },
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=CurrentDenialLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["social"]
        self.assertEqual(field["value"], "Smoking: 현재 비흡연")
        self.assertEqual(field["status"], "filled")
        social = result["api2"]["clinical_record"]["social_history"]
        self.assertIsNone(social["smoking"]["state"])
        self.assertEqual(
            social["alcohol"]["alcohol_status"]["status"],
            "not_mentioned",
        )

    def test_social_history_flags_unsupported_never_smoker_label(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class UnsupportedNeverLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {
                    "clinical_record": {
                        "social_history": {
                            "text": "Smoking: Never",
                            "smoking": {
                                "text": "Smoking: Never",
                                "state": "Never smoker",
                                "smoking_status": {
                                    "raw_value": "안 피워요",
                                    "status": "confirmed",
                                    "evidence": {
                                        "source_segment_id": "seg_0002"
                                    },
                                },
                            },
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "담배 피우세요?",
                    },
                    {
                        "id": "seg_0002",
                        "start": 1.0,
                        "end": 2.0,
                        "text": "안 피워요.",
                    },
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=UnsupportedNeverLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        self.assertEqual(
            result["draft"]["fields"]["social"]["status"],
            "needs_review",
        )

    def test_social_history_converts_cigarettes_to_pack_years_with_approved_rule(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class CigaretteCountLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )

                def measurement(raw_value, value):
                    return {
                        "raw_value": raw_value,
                        "value": value,
                        "status": "confirmed",
                        "evidence": {"source_segment_id": "seg_0002"},
                    }

                return {
                    "clinical_record": {
                        "social_history": {
                            "text": "Smoking: Current, 10 PY",
                            "smoking": {
                                "text": "Smoking: Current, 10 PY",
                                "state": "Current smoker",
                                "smoking_status": measurement(
                                    "하루 10개비씩 20년 피웠어요", 1
                                ),
                                "cigarettes_per_day": measurement(
                                    "하루 10개비", 10
                                ),
                                "duration_years": measurement("20년", 20),
                            },
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "담배를 얼마나 오래, 하루 몇 개비 피우세요?",
                    },
                    {
                        "id": "seg_0002",
                        "start": 1.0,
                        "end": 2.0,
                        "text": "하루 10개비씩 20년 피웠어요.",
                    },
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=CigaretteCountLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["social"]
        self.assertEqual(field["status"], "filled")
        smoking = result["api2"]["clinical_record"]["social_history"][
            "smoking"
        ]
        self.assertEqual(smoking["pack_years"], 10)
        self.assertEqual(
            smoking["pack_years_provenance"]["formula"],
            "cigarettes_per_day / 20 * duration_years",
        )

    def test_social_history_flags_pack_year_mismatch_and_inferred_alcohol_risk(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        def atom(raw_value, *, value=None):
            result = {
                "raw_value": raw_value,
                "status": "confirmed",
                "evidence": {"source_segment_id": "seg_0001"},
            }
            if value is not None:
                result["value"] = value
            return result

        cases = (
            {
                "name": "mismatched pack years",
                "text": "하루 반 갑씩 10년 피웠어요.",
                "social_history": {
                    "text": "Smoking: Current, 6 PY",
                    "smoking": {
                        "text": "Smoking: Current, 6 PY",
                        "state": "Current smoker",
                        "smoking_status": atom("하루 반 갑씩 10년 피웠어요"),
                        "packs_per_day": atom("하루 반 갑", value=0.5),
                        "duration_years": atom("10년", value=10),
                    },
                },
            },
            {
                "name": "inferred alcohol risk",
                "text": "일주일에 세 번 소주 한 병씩 마셔요.",
                "social_history": {
                    "text": "Alcohol: Heavy drinker",
                    "alcohol": {
                        "text": "Alcohol: Heavy drinker",
                        "alcohol_status": atom(
                            "일주일에 세 번 소주 한 병씩 마셔요"
                        ),
                        "frequency": atom("일주일에 세 번"),
                        "type": atom("소주"),
                        "amount_per_occasion": atom("한 병", value=1),
                    },
                },
            },
        )

        for case in cases:
            class InvalidSocialLlm(ClinicalLlmClient):
                def generate_json(self, **kwargs):
                    if kwargs["output_label"] != "clinical record":
                        raise AssertionError(
                            f"unexpected LLM task: {kwargs['output_label']}"
                        )
                    return {
                        "clinical_record": {
                            "social_history": case["social_history"]
                        },
                        "unresolved_questions": [],
                    }

            with self.subTest(case["name"]):
                result = run_clinical_workflow(
                    {
                        "segments": [
                            {
                                "id": "seg_0001",
                                "start": 0.0,
                                "end": 1.0,
                                "text": case["text"],
                            }
                        ]
                    },
                    retriever=_EmptyRetriever(),
                    clinical_extractor=LlamaServerClinicalExtractor(
                        "http://unused.local", llm_client=InvalidSocialLlm()
                    ),
                    query_expander=Expander(),
                    medical_query_resolver=_RecordingResolver(),
                )

                self.assertEqual(
                    result["draft"]["fields"]["social"]["status"],
                    "needs_review",
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

    def test_ros_preserves_model_authored_positive_and_negative_assertions(self):
        cases = (
            ("seg_0002", "기침", "PRESENT"),
            ("seg_0002", "노란 가래", "PRESENT"),
            ("seg_0002", "오한은 좀 들어요", "PRESENT"),
            ("seg_0002", "열은 없는데", "DENIED"),
            ("seg_0004", "숨은 전혀 안 차고", "DENIED"),
            ("seg_0004", "가슴도 안 아파요", "DENIED"),
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
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class RosLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {
                    "clinical_record": {
                        "review_of_systems": {
                            "text": (
                                "Cough(+), Sputum(+), Chill(+), Fever(-), "
                                "Dyspnea(-), Chest pain(-)"
                            ),
                            "items": [
                                {
                                    "symptom": {
                                        "raw_value": raw_value,
                                        "status": "confirmed",
                                        "evidence": {
                                            "source_segment_id": segment_id
                                        },
                                    },
                                    "assertion": assertion,
                                }
                                for segment_id, raw_value, assertion in cases
                            ],
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "기침이나 가래는 좀 어떠세요? 발열이나 오한도 있나요?",
                    },
                    {
                        "id": "seg_0002",
                        "start": 1.0,
                        "end": 2.0,
                        "text": "기침이랑 노란 가래가 심하고요. 열은 없는데 오한은 좀 들어요.",
                    },
                    {
                        "id": "seg_0003",
                        "start": 2.0,
                        "end": 3.0,
                        "text": "가슴 통증이나 호흡곤란은요?",
                    },
                    {
                        "id": "seg_0004",
                        "start": 3.0,
                        "end": 4.0,
                        "text": "숨은 전혀 안 차고, 가슴도 안 아파요.",
                    },
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=RosLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["review-of-systems"]
        self.assertEqual(
            field["value"],
            "Cough(+), Sputum(+), Chill(+), Fever(-), Dyspnea(-), Chest pain(-)",
        )
        self.assertEqual(
            [item["assertion"] for item in result["api2"]["clinical_record"][
                "review_of_systems"
            ]["items"]],
            ["PRESENT", "PRESENT", "PRESENT", "DENIED", "DENIED", "DENIED"],
        )
        self.assertEqual(
            [item["segment_id"] for item in field["evidence"]],
            ["seg_0002", "seg_0004"],
        )

    def test_ros_flags_hpi_details_in_symbol_only_display(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class DetailedRosLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {
                    "clinical_record": {
                        "review_of_systems": {
                            "text": "Cough for 4 days(+)",
                            "items": [
                                {
                                    "symptom": {
                                        "raw_value": "4일 전부터 기침이 심해요",
                                        "status": "confirmed",
                                        "evidence": {
                                            "source_segment_id": "seg_0001"
                                        },
                                    },
                                    "assertion": "PRESENT",
                                }
                            ],
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "4일 전부터 기침이 심해요.",
                    }
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=DetailedRosLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["review-of-systems"]
        self.assertEqual(field["value"], "Cough for 4 days(+)")
        self.assertEqual(field["status"], "needs_review")

    def test_ros_flags_duplicate_symptom_entries_without_deleting_them(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": "The patient has a cough.",
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class DuplicateRosLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                symptom = {
                    "raw_value": "기침이 있어요",
                    "status": "confirmed",
                    "evidence": {"source_segment_id": "seg_0001"},
                }
                return {
                    "clinical_record": {
                        "review_of_systems": {
                            "text": "Cough(+), Cough(+)",
                            "items": [
                                {"symptom": symptom, "assertion": "PRESENT"},
                                {"symptom": symptom, "assertion": "PRESENT"},
                            ],
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "기침이 있어요.",
                    }
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=DuplicateRosLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["review-of-systems"]
        self.assertEqual(field["value"], "Cough(+), Cough(+)")
        self.assertEqual(field["status"], "needs_review")

    def test_ros_flags_display_label_that_does_not_match_grounded_translation(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del segments, covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": "seg_0001",
                            "translated_text_en": "The patient has a cough.",
                        }
                    ],
                    "items": [],
                }

        class WrongLabelRosLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {
                    "clinical_record": {
                        "review_of_systems": {
                            "text": "Fever(+)",
                            "items": [
                                {
                                    "symptom": {
                                        "raw_value": "기침이 있어요",
                                        "status": "confirmed",
                                        "evidence": {
                                            "source_segment_id": "seg_0001"
                                        },
                                    },
                                    "assertion": "PRESENT",
                                }
                            ],
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "기침이 있어요.",
                    }
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=WrongLabelRosLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["review-of-systems"]
        self.assertEqual(field["value"], "Fever(+)")
        self.assertEqual(field["status"], "needs_review")

    def test_ros_not_mentioned_keeps_null_text_and_empty_items(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class NoRosLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {
                    "clinical_record": {},
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "진료를 시작하겠습니다.",
                    }
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=NoRosLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        self.assertEqual(
            result["api2"]["clinical_record"]["review_of_systems"],
            {"text": None, "items": []},
        )
        field = result["draft"]["fields"]["review-of-systems"]
        self.assertEqual(field["value"], "")
        self.assertEqual(field["status"], "empty")

    def test_ros_preserves_uncertain_symptom_with_question_mark(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del segments, covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": "seg_0001",
                            "translated_text_en": (
                                "The patient is unsure whether there is fever."
                            ),
                        }
                    ],
                    "items": [],
                }

        class UncertainRosLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {
                    "clinical_record": {
                        "review_of_systems": {
                            "text": "Fever(?)",
                            "items": [
                                {
                                    "symptom": {
                                        "raw_value": "열이 있는지 잘 모르겠어요",
                                        "status": "needs_confirmation",
                                        "evidence": {
                                            "source_segment_id": "seg_0001"
                                        },
                                    },
                                    "assertion": "UNCERTAIN",
                                }
                            ],
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "열이 있는지 잘 모르겠어요.",
                    }
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=UncertainRosLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["review-of-systems"]
        self.assertEqual(field["value"], "Fever(?)")
        self.assertEqual(field["status"], "needs_review")

    def test_physical_exam_preserves_grounded_system_findings(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                translations = {
                    "seg_0001": (
                        "On auscultation, bilateral breath sounds are clear "
                        "without rales or wheezing."
                    ),
                    "seg_0002": (
                        "On abdominal palpation, right lower quadrant "
                        "tenderness and rebound tenderness are present."
                    ),
                }
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": translations[segment["id"]],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class PhysicalExamLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {
                    "clinical_record": {
                        "physical_examination": {
                            "text": (
                                "Chest: Clear breath sounds without rale/wheezing\n"
                                "Abdomen: RLQ tenderness(+), Rebound tenderness(+)"
                            ),
                            "findings": [
                                {
                                    "system": "Chest",
                                    "finding": {
                                        "raw_value": "양쪽 폐소리 깨끗하고",
                                        "status": "confirmed",
                                        "evidence": {
                                            "source_segment_id": "seg_0001"
                                        },
                                    },
                                    "assertion": "PRESENT",
                                },
                                {
                                    "system": "Chest",
                                    "finding": {
                                        "raw_value": "수포음과 천명은 없습니다",
                                        "status": "confirmed",
                                        "evidence": {
                                            "source_segment_id": "seg_0001"
                                        },
                                    },
                                    "assertion": "ABSENT",
                                },
                                {
                                    "system": "Abdomen",
                                    "finding": {
                                        "raw_value": "우하복부 압통과 반발통이 확인됩니다",
                                        "status": "confirmed",
                                        "evidence": {
                                            "source_segment_id": "seg_0002"
                                        },
                                    },
                                    "assertion": "PRESENT",
                                },
                            ],
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "청진 결과 양쪽 폐소리 깨끗하고 수포음과 천명은 없습니다.",
                    },
                    {
                        "id": "seg_0002",
                        "start": 1.0,
                        "end": 2.0,
                        "text": "복부 촉진상 우하복부 압통과 반발통이 확인됩니다.",
                    },
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=PhysicalExamLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        self.assertEqual(
            result["api2"]["clinical_record"]["physical_examination"]["text"],
            "Chest: Clear breath sounds without rale/wheezing\n"
            "Abdomen: RLQ tenderness(+), Rebound tenderness(+)",
        )
        field = result["draft"]["fields"]["physical"]
        self.assertEqual(
            field["value"],
            "Chest: Clear breath sounds without rale/wheezing\n"
            "Abdomen: RLQ tenderness(+), Rebound tenderness(+)",
        )
        self.assertEqual(field["status"], "filled")
        self.assertEqual(len(field["evidence"]), 2)

    def test_physical_exam_flags_patient_symptom_as_inferred_finding(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del segments, covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [{
                        "segment_id": "seg_0001",
                        "translated_text_en": "The patient reports abdominal pain.",
                    }],
                    "items": [],
                }

        class InferredPhysicalExamLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {
                    "clinical_record": {
                        "physical_examination": {
                            "text": "Abdomen: Tenderness(+)",
                            "findings": [{
                                "system": "Abdomen",
                                "finding": {
                                    "raw_value": "배가 아파요",
                                    "status": "confirmed",
                                    "evidence": {
                                        "source_segment_id": "seg_0001"
                                    },
                                },
                                "assertion": "PRESENT",
                            }],
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [{
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "배가 아파요.",
                }]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=InferredPhysicalExamLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["physical"]
        self.assertEqual(field["value"], "Abdomen: Tenderness(+)")
        self.assertEqual(field["status"], "needs_review")
        workflow_v2 = to_clinical_workflow_v2(result)
        issue = next(
            item
            for item in workflow_v2["validation"]["issues"]
            if item["rule_id"] == "G09"
        )
        self.assertEqual(issue["severity"], "BLOCK")
        self.assertEqual(issue["field_id"], "physical_examination")
        self.assertEqual(
            workflow_v2["draft"]["fields"]["physical_examination"]["value"],
            "Abdomen: Tenderness(+)",
        )

    def test_physical_exam_preserves_uncertain_limited_examination(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del segments, covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [{
                        "segment_id": "seg_0001",
                        "translated_text_en": (
                            "Abdominal palpation was limited due to severe pain."
                        ),
                    }],
                    "items": [],
                }

        class LimitedPhysicalExamLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {
                    "clinical_record": {
                        "physical_examination": {
                            "text": "Abdomen: Palpation limited due to severe pain",
                            "findings": [{
                                "system": "Abdomen",
                                "finding": {
                                    "raw_value": "복부 촉진은 통증 때문에 제한적입니다",
                                    "status": "needs_confirmation",
                                    "evidence": {
                                        "source_segment_id": "seg_0001"
                                    },
                                },
                                "assertion": "UNCERTAIN",
                            }],
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [{
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "복부 촉진은 통증 때문에 제한적입니다.",
                }]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=LimitedPhysicalExamLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["physical"]
        self.assertEqual(
            field["value"], "Abdomen: Palpation limited due to severe pain"
        )
        self.assertEqual(field["status"], "needs_review")

    def test_physical_exam_not_mentioned_keeps_null_text_and_empty_findings(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class NoPhysicalExamLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {"clinical_record": {}, "unresolved_questions": []}

        result = run_clinical_workflow(
            {
                "segments": [{
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "진료를 시작하겠습니다.",
                }]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=NoPhysicalExamLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        self.assertEqual(
            result["api2"]["clinical_record"]["physical_examination"],
            {"text": None, "findings": []},
        )
        field = result["draft"]["fields"]["physical"]
        self.assertEqual(field["value"], "")
        self.assertEqual(field["status"], "empty")

    def test_impression_preserves_confirmed_and_multiple_rule_out_diagnoses(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": (
                                "The test confirms a urinary tract infection."
                                if segment["id"] == "seg_0001"
                                else "We need to differentiate appendicitis and diverticulitis."
                            ),
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class ImpressionLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )

                def diagnosis(raw_value, segment_id, status):
                    return {
                        "raw_value": raw_value,
                        "status": status,
                        "evidence": {"source_segment_id": segment_id},
                    }

                return {
                    "clinical_record": {
                        "impression": {
                            "text": (
                                "Urinary tract infection\n"
                                "R/O Acute appendicitis\n"
                                "R/O Diverticulitis"
                            ),
                            "items": [
                                {
                                    "diagnosis": diagnosis(
                                        "검사 결과 요로감염입니다",
                                        "seg_0001",
                                        "confirmed",
                                    ),
                                    "certainty": "CONFIRMED",
                                },
                                {
                                    "diagnosis": diagnosis(
                                        "충수염 가능성",
                                        "seg_0002",
                                        "needs_confirmation",
                                    ),
                                    "certainty": "SUSPECTED",
                                },
                                {
                                    "diagnosis": diagnosis(
                                        "게실염도 감별",
                                        "seg_0002",
                                        "needs_confirmation",
                                    ),
                                    "certainty": "RULE_OUT",
                                },
                            ],
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "검사 결과 요로감염입니다.",
                    },
                    {
                        "id": "seg_0002",
                        "start": 1.0,
                        "end": 2.0,
                        "text": "충수염 가능성을 봐야 하고 게실염도 감별해야겠습니다.",
                    },
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=ImpressionLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        impression = result["api2"]["clinical_record"]["impression"]
        self.assertEqual(
            [item["certainty"] for item in impression["items"]],
            ["CONFIRMED", "SUSPECTED", "RULE_OUT"],
        )
        field = result["draft"]["fields"]["impression"]
        self.assertEqual(
            field["value"],
            "Urinary tract infection\nR/O Acute appendicitis\nR/O Diverticulitis",
        )
        self.assertEqual(field["status"], "needs_review")
        self.assertEqual(len(field["evidence"]), 2)

    def test_impression_not_assessed_keeps_null_text_and_empty_items(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class NoImpressionLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {"clinical_record": {}, "unresolved_questions": []}

        result = run_clinical_workflow(
            {
                "segments": [{
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "기침은 언제부터 시작됐나요?",
                }]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=NoImpressionLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        self.assertEqual(
            result["api2"]["clinical_record"]["impression"],
            {"text": None, "items": []},
        )
        field = result["draft"]["fields"]["impression"]
        self.assertEqual(field["value"], "")
        self.assertEqual(field["status"], "empty")

    def test_outcome_accepts_an_explicit_final_admission(self):
        source = {
            "text": "입원",
            "category": "Admission",
            "information_status": "PRESENT",
            "decision": {
                "raw_value": "외과로 입원해서 치료하겠습니다",
                "status": "confirmed",
                "evidence": {"source_segment_id": "seg_0001"},
            },
        }
        api3 = {
            "segments": [{
                "id": "seg_0001",
                "start": 0.0,
                "end": 1.0,
                "raw_text": "외과로 입원해서 치료하겠습니다.",
                "annotations": [],
            }]
        }

        field = build_draft(
            {"clinical_record": {"outcome": source}}, api3
        )["fields"]["outcome"]

        self.assertEqual(field["value"], "입원")
        self.assertEqual(field["status"], "filled")
        self.assertEqual(field["evidence"][0]["segment_id"], "seg_0001")

    def test_outcome_does_not_promote_a_conditional_admission_plan(self):
        source = {
            "text": "입원",
            "category": "Admission",
            "information_status": "PRESENT",
            "decision": {
                "raw_value": "CT 결과 보고 입원 여부를 결정하겠습니다",
                "status": "confirmed",
                "evidence": {"source_segment_id": "seg_0001"},
            },
        }
        api3 = {
            "segments": [{
                "id": "seg_0001",
                "start": 0.0,
                "end": 1.0,
                "raw_text": "CT 결과 보고 입원 여부를 결정하겠습니다.",
                "annotations": [],
            }]
        }

        field = build_draft(
            {"clinical_record": {"outcome": source}}, api3
        )["fields"]["outcome"]

        self.assertEqual(field["value"], "진료 진행 중")
        self.assertEqual(field["status"], "needs_review")

    def test_outcome_does_not_infer_death_from_cardiac_arrest_or_cpr(self):
        source = {
            "text": "사망",
            "category": "Death",
            "information_status": "PRESENT",
            "decision": {
                "raw_value": "심정지로 CPR을 시행했습니다",
                "status": "confirmed",
                "evidence": {"source_segment_id": "seg_0001"},
            },
        }
        api3 = {
            "segments": [{
                "id": "seg_0001",
                "start": 0.0,
                "end": 1.0,
                "raw_text": "심정지로 CPR을 시행했습니다.",
                "annotations": [],
            }]
        }

        field = build_draft(
            {"clinical_record": {"outcome": source}}, api3
        )["fields"]["outcome"]

        self.assertEqual(field["value"], "진료 진행 중")
        self.assertEqual(field["status"], "needs_review")

    def test_outcome_not_assessed_remains_empty(self):
        source = {
            "text": None,
            "category": None,
            "information_status": "NOT_ASSESSED",
            "decision": {
                "raw_value": None,
                "status": "not_mentioned",
                "evidence": None,
            },
        }

        field = build_draft(
            {"clinical_record": {"outcome": source}}, {"segments": []}
        )["fields"]["outcome"]

        self.assertEqual(field["value"], "")
        self.assertEqual(field["status"], "empty")

    def test_treatment_plan_preserves_grounded_categorized_actions(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                translations = {
                    "seg_0001": (
                        "We will perform blood tests and an abdomen CT."
                    ),
                    "seg_0002": (
                        "Keep NPO and administer IV fluids and analgesics. "
                        "GS consultation will be decided after the CT result."
                    ),
                }
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": translations[segment["id"]],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class TreatmentPlanLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )

                def action(raw_value, segment_id):
                    return {
                        "raw_value": raw_value,
                        "status": "confirmed",
                        "evidence": {"source_segment_id": segment_id},
                    }

                return {
                    "clinical_record": {
                        "treatment_plan": {
                            "text": (
                                "Diagnostic Workup: Blood tests, Abdomen CT\n"
                                "Medication / Procedure: NPO, IV fluids, Analgesics\n"
                                "Consultation: GS consult pending CT results"
                            ),
                            "items": [
                                {
                                    "category": "Diagnostic Workup",
                                    "action": action(
                                        "혈액 검사하고 복부 CT 촬영을 진행하겠습니다",
                                        "seg_0001",
                                    ),
                                    "assertion": "PRESENT",
                                    "plan_status": "ORDERED",
                                },
                                {
                                    "category": "Medication / Procedure",
                                    "action": action(
                                        "금식 유지하시고 진통제랑 수액을 IV로 투여하겠습니다",
                                        "seg_0002",
                                    ),
                                    "assertion": "PRESENT",
                                    "plan_status": "PLANNED",
                                },
                                {
                                    "category": "Consultation",
                                    "action": action(
                                        "CT 결과 보고 외과 협진 여부 결정할게요",
                                        "seg_0002",
                                    ),
                                    "assertion": "UNCERTAIN",
                                    "plan_status": "CONDITIONAL",
                                },
                            ],
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 1.0,
                        "text": "혈액 검사하고 복부 CT 촬영을 진행하겠습니다.",
                    },
                    {
                        "id": "seg_0002",
                        "start": 1.0,
                        "end": 2.0,
                        "text": (
                            "금식 유지하시고 진통제랑 수액을 IV로 투여하겠습니다. "
                            "CT 결과 보고 외과 협진 여부 결정할게요."
                        ),
                    },
                ]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=TreatmentPlanLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["treatment-plan"]
        self.assertEqual(
            field["value"],
            "Diagnostic Workup: Blood tests, Abdomen CT\n"
            "Medication / Procedure: NPO, IV fluids, Analgesics\n"
            "Consultation: GS consult pending CT results",
        )
        self.assertEqual(field["status"], "needs_review")
        self.assertEqual(len(field["evidence"]), 2)

    def test_treatment_plan_blocks_unstated_order_without_deleting_text(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del segments, covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [{
                        "segment_id": "seg_0001",
                        "translated_text_en": "We will perform a blood test.",
                    }],
                    "items": [],
                }

        class UnsupportedPlanLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {
                    "clinical_record": {
                        "treatment_plan": {
                            "text": "Diagnostic Workup: Blood test, Chest X-ray",
                            "items": [{
                                "category": "Diagnostic Workup",
                                "action": {
                                    "raw_value": "혈액 검사하겠습니다",
                                    "status": "confirmed",
                                    "evidence": {
                                        "source_segment_id": "seg_0001"
                                    },
                                },
                                "assertion": "PRESENT",
                                "plan_status": "ORDERED",
                            }],
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [{
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "혈액 검사하겠습니다.",
                }]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=UnsupportedPlanLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["treatment-plan"]
        self.assertEqual(
            field["value"], "Diagnostic Workup: Blood test, Chest X-ray"
        )
        self.assertEqual(field["status"], "needs_review")
        workflow_v2 = to_clinical_workflow_v2(result)
        issue = next(
            item
            for item in workflow_v2["validation"]["issues"]
            if item["rule_id"] == "G08"
        )
        self.assertEqual(issue["severity"], "BLOCK")
        self.assertEqual(issue["field_id"], "treatment_plan")
        self.assertEqual(
            workflow_v2["draft"]["fields"]["treatment_plan"]["value"],
            "Diagnostic Workup: Blood test, Chest X-ray",
        )

    def test_treatment_plan_preserves_conditional_disposition(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del segments, covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [{
                        "segment_id": "seg_0001",
                        "translated_text_en": (
                            "We will decide on admission after reviewing the CT result."
                        ),
                    }],
                    "items": [],
                }

        class ConditionalPlanLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {
                    "clinical_record": {
                        "treatment_plan": {
                            "text": (
                                "Disposition / Safety-netting: "
                                "Admission pending CT results"
                            ),
                            "items": [{
                                "category": "Disposition / Safety-netting",
                                "action": {
                                    "raw_value": "CT 결과 보고 입원 여부 결정합시다",
                                    "status": "needs_confirmation",
                                    "evidence": {
                                        "source_segment_id": "seg_0001"
                                    },
                                },
                                "assertion": "UNCERTAIN",
                                "plan_status": "CONDITIONAL",
                            }],
                        }
                    },
                    "unresolved_questions": [],
                }

        result = run_clinical_workflow(
            {
                "segments": [{
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "CT 결과 보고 입원 여부 결정합시다.",
                }]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=ConditionalPlanLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        field = result["draft"]["fields"]["treatment-plan"]
        self.assertEqual(
            field["value"],
            "Disposition / Safety-netting: Admission pending CT results",
        )
        self.assertEqual(field["status"], "needs_review")

    def test_treatment_plan_not_mentioned_keeps_null_text_and_empty_items(self):
        class Expander:
            def expand(self, segments, *, covered_spans):
                del covered_spans
                return {
                    "status": "available",
                    "fallback_used": False,
                    "translated_segments": [
                        {
                            "segment_id": segment["id"],
                            "translated_text_en": segment["text"],
                        }
                        for segment in segments
                    ],
                    "items": [],
                }

        class NoTreatmentPlanLlm(ClinicalLlmClient):
            def generate_json(self, **kwargs):
                if kwargs["output_label"] != "clinical record":
                    raise AssertionError(
                        f"unexpected LLM task: {kwargs['output_label']}"
                    )
                return {"clinical_record": {}, "unresolved_questions": []}

        result = run_clinical_workflow(
            {
                "segments": [{
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "진료를 시작하겠습니다.",
                }]
            },
            retriever=_EmptyRetriever(),
            clinical_extractor=LlamaServerClinicalExtractor(
                "http://unused.local", llm_client=NoTreatmentPlanLlm()
            ),
            query_expander=Expander(),
            medical_query_resolver=_RecordingResolver(),
        )

        self.assertEqual(
            result["api2"]["clinical_record"]["treatment_plan"],
            {"text": None, "items": []},
        )
        field = result["draft"]["fields"]["treatment-plan"]
        self.assertEqual(field["value"], "")
        self.assertEqual(field["status"], "empty")

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
        expander = _SequentialExpander()
        extractor = _StagedExtractor(expander)
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
