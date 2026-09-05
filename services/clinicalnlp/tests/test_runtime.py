import unittest

from clinicalnlp_api3.medical_query_resolver import (
    QueryResolution,
    QueryResolutionTelemetry,
)
from clinicalnlp_api3.runtime import create_clinical_runtime


class _EmptyRetriever:
    def retrieve(self, *, raw_text, context):
        return []


class _SyntheticQueryExpander:
    def expand(self, segments, *, covered_spans=None):
        return {
            "status": "available",
            "fallback_used": False,
            "method": "SYNTHETIC_TRANSLATION",
            "translated_segments": [
                {
                    "segment_id": segment["id"],
                    "translated_text_en": "Abdominal pain.",
                }
                for segment in segments
            ],
            "items": [],
            "partial": False,
            "failed_segment_ids": [],
            "_telemetry": {
                "translation_ms": 150.0,
                "translation_calls": 1,
                "translation_batch_count": 1,
                "translation_worker_count": 1,
                "translation_retry_split_count": 0,
                "translation_rate_limit_count": 0,
                "translation_batches": [
                    {
                        "batch_index": 0,
                        "target_segment_count": 1,
                        "context_segment_count": 1,
                        "request_count": 1,
                        "retry_split_count": 0,
                        "rate_limit_count": 0,
                        "failed_segment_count": 0,
                        "elapsed_ms": 140.0,
                    }
                ],
                "translation_provider_calls": 1,
                "translation_network_retries": 0,
                "translation_http_ms": 140.0,
                "translation_provider_ms": 120.0,
                "translation_provider_load_ms": 3.0,
                "translation_prompt_eval_ms": 30.0,
                "translation_token_eval_ms": 80.0,
                "translation_unattributed_http_ms": 20.0,
            },
        }


class _SyntheticClinicalExtractor:
    def __init__(self):
        self.compact_compare_calls = 0
        self.extract_calls = 0

    def extract(self, payload):
        self.extract_calls += 1
        return {
            "schema_version": "clinical-record-v2",
            "clinical_record": {
                "chief_complaint": {
                    "raw_value": "배가 아파요",
                    "status": "confirmed",
                    "evidence": {"source_segment_id": "seg_0001"},
                }
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

    def compare_compact_record(self, payload, candidate_snapshots):
        self.compact_compare_calls += 1
        fields = {
            field_id: {
                "generation_status": "NOT_MENTIONED",
                "text": None,
                "fact_refs": [],
            }
            for field_id in (
                "chief_complaint",
                "pain_assessment",
                "history_of_present_illness",
                "past_history",
                "medications",
                "drug_allergy",
                "social_history",
                "review_of_systems",
                "physical_examination",
                "impression",
                "treatment_plan",
                "outcome",
            )
        }
        fields["chief_complaint"] = {
            "generation_status": "GENERATED",
            "text": "Abdominal pain.",
            "fact_refs": ["f1"],
        }
        return {
            "prompt_version": "clinical-record-compact-v3.1",
            "record": {
                "schema_version": "clinical-record-compact-v3",
                "facts": {
                    "f1": {
                        "type": "NARRATIVE",
                        "text": "배가 아파요",
                        "assertion": "PRESENT",
                        "segments": ["seg_0001"],
                    }
                },
                "fields": fields,
            },
            "validation": {
                "schema_version": "clinical-record-compact-validation-v1",
                "status": "PASS",
                "processing_status": "completed",
                "issues": [],
            },
            "generation": {
                "fact_chunk_count": 3,
                "fact_chunk_worker_count": 3,
                "field_group_call_count": 0,
                "length_fallback_count": 0,
                "repair_count": 1,
                "regeneration_count": 0,
                "fact_recovery_count": 1,
                "fact_recovery_reasons": [
                    "fact[2]: TEXT_FACT_WITHOUT_CANDIDATE_REF_DOWNGRADED"
                ],
                "fact_targeted_retry_count": 1,
                "fact_preserved_count": 7,
                "field_reference_retry_count": 1,
                "field_preserved_count": 5,
                "failed_segment_count": 0,
                "provider_call_count": 2,
                "network_retry_count": 1,
                "http_elapsed_ms": 140.0,
                "provider_total_ms": 120.0,
                "provider_load_ms": 4.0,
                "provider_prompt_eval_ms": 30.0,
                "provider_eval_ms": 80.0,
                "unattributed_http_ms": 20.0,
            },
        }


class _SyntheticMedicalQueryResolver:
    def resolve(self, document):
        return QueryResolution(
            mode="umls_primary",
            status="complete",
            policy_version="synthetic-policy-v1",
            telemetry=QueryResolutionTelemetry(
                umls_ms=30.0,
                umls_model_load_ms=50.0,
                umls_mention_detection_ms=8.0,
                umls_linking_ms=12.0,
                umls_extraction_ms=20.0,
                umls_worker_overhead_ms=10.0,
                umls_worker_cold_start_overhead_ms=10.0,
                umls_worker_batch_count=1,
                umls_worker_fallback_batch_count=0,
                umls_worker_cold_start_batch_count=1,
                umls_input_segment_count=1,
                umls_input_character_count=20,
                umls_detected_span_count=2,
                umls_detected_span_character_count=11,
                umls_linker_document_count=1,
                vector_ms=20.0,
                vector_statement_count=3,
                exact_search_batch_count=2,
                exact_search_query_count=3,
                exact_search_hit_count=1,
                vector_fallback_batch_count=1,
                vector_fallback_query_count=1,
                vector_fallback_hit_count=1,
                umls_surface_query_count=1,
                umls_canonical_query_count=1,
                vector_collection_ms=(
                    ("drug_terms", 12.5),
                    ("emergency_terms", 7.5),
                ),
                vector_collection_statement_counts=(
                    ("drug_terms", 2),
                    ("emergency_terms", 1),
                ),
                vector_collection_batch_counts=(
                    ("drug_terms", 1),
                    ("emergency_terms", 1),
                ),
                vector_collection_query_counts=(
                    ("drug_terms", 1),
                    ("emergency_terms", 1),
                ),
                vector_collection_candidate_counts=(
                    ("drug_terms", 2),
                    ("emergency_terms", 1),
                ),
                vector_collection_empty_query_counts=(
                    ("drug_terms", 0),
                    ("emergency_terms", 0),
                ),
                vector_partition_ms=(
                    ("drug_terms", "ingredient", 6.0),
                    ("drug_terms", "product", 6.0),
                    ("emergency_terms", "all", 7.5),
                ),
                vector_partition_result_counts=(
                    ("drug_terms", "ingredient", 1),
                    ("drug_terms", "product", 1),
                    ("emergency_terms", "all", 1),
                ),
            ),
        )


class ClinicalDraftRuntimeTests(unittest.TestCase):
    def test_generate_draft_returns_the_versioned_reviewable_interface(self):
        runtime = create_clinical_runtime(
            retriever=_EmptyRetriever(),
            clinical_extractor=_SyntheticClinicalExtractor(),
            query_expander=_SyntheticQueryExpander(),
            medical_query_resolver=_SyntheticMedicalQueryResolver(),
        )

        result = runtime.generate_draft(
            {
                "language": "ko",
                "segments": [
                    {
                        "id": "seg_0001",
                        "start": 0.0,
                        "end": 2.0,
                        "text": "배가 아파요",
                    }
                ],
            }
        )

        self.assertEqual(result["schema_version"], "clinical-workflow-v2")
        self.assertEqual(result["record_status"], "DRAFT")
        self.assertEqual(result["workflow_phase"], "DRAFT_GENERATION")
        self.assertIsNone(result["completed_at"])
        self.assertEqual(
            result["draft"]["fields"]["chief_complaint"]["value"],
            "Abdominal pain.",
        )
        self.assertEqual(
            set(result["telemetry"]),
            {
                "translation_ms",
                "translation_calls",
                "translation_batch_count",
                "translation_worker_count",
                "translation_retry_split_count",
                "translation_partial_retry_count",
                "translation_preserved_segment_count",
                "translation_retry_reasons",
                "translation_rate_limit_count",
                "translation_batches",
                "translation_provider_calls",
                "translation_network_retries",
                "translation_http_ms",
                "translation_provider_ms",
                "translation_provider_load_ms",
                "translation_prompt_eval_ms",
                "translation_token_eval_ms",
                "translation_unattributed_http_ms",
                "umls_ms",
                "umls_model_load_ms",
                "umls_mention_detection_ms",
                "umls_linking_ms",
                "umls_extraction_ms",
                "umls_worker_overhead_ms",
                "umls_worker_cold_start_overhead_ms",
                "umls_worker_batch_count",
                "umls_worker_fallback_batch_count",
                "umls_worker_cold_start_batch_count",
                "umls_input_segment_count",
                "umls_input_character_count",
                "umls_detected_span_count",
                "umls_detected_span_character_count",
                "umls_linker_document_count",
                "dictionary_ms",
                "vector_ms",
                "exact_statement_count",
                "vector_statement_count",
                "search_cache_hit_count",
                "exact_search_batch_count",
                "exact_search_query_count",
                "exact_search_hit_count",
                "vector_fallback_batch_count",
                "vector_fallback_query_count",
                "vector_fallback_hit_count",
                "vector_fallback_empty_count",
                "umls_surface_query_count",
                "umls_canonical_query_count",
                "semantic_fallback_query_count",
                "ngram_fallback_query_count",
                "vector_drug_terms_ms",
                "vector_drug_terms_statement_count",
                "vector_drug_terms_batch_count",
                "vector_drug_terms_query_count",
                "vector_drug_terms_candidate_count",
                "vector_drug_terms_empty_query_count",
                "vector_drug_terms_ingredient_ms",
                "vector_drug_terms_ingredient_result_count",
                "vector_drug_terms_product_ms",
                "vector_drug_terms_product_result_count",
                "vector_procedure_terms_ms",
                "vector_procedure_terms_statement_count",
                "vector_procedure_terms_batch_count",
                "vector_procedure_terms_query_count",
                "vector_procedure_terms_candidate_count",
                "vector_procedure_terms_empty_query_count",
                "vector_anatomy_terms_ms",
                "vector_anatomy_terms_statement_count",
                "vector_anatomy_terms_batch_count",
                "vector_anatomy_terms_query_count",
                "vector_anatomy_terms_candidate_count",
                "vector_anatomy_terms_empty_query_count",
                "vector_emergency_terms_ms",
                "vector_emergency_terms_statement_count",
                "vector_emergency_terms_batch_count",
                "vector_emergency_terms_query_count",
                "vector_emergency_terms_candidate_count",
                "vector_emergency_terms_empty_query_count",
                "clinical_extraction_ms",
                "clinical_llm_fact_chunk_count",
                "clinical_llm_fact_chunk_worker_count",
                "clinical_llm_field_group_call_count",
                "clinical_llm_length_fallback_count",
                "clinical_llm_repair_count",
                "clinical_llm_regeneration_count",
                "clinical_llm_fact_recovery_count",
                "clinical_llm_fact_recovery_reasons",
                "clinical_llm_fact_targeted_retry_count",
                "clinical_llm_fact_preserved_count",
                "clinical_llm_field_reference_retry_count",
                "clinical_llm_field_preserved_count",
                "clinical_llm_validation_failure_reasons",
                "clinical_llm_failed_segment_count",
                "clinical_llm_provider_calls",
                "clinical_llm_network_retries",
                "clinical_llm_http_ms",
                "clinical_llm_provider_ms",
                "clinical_llm_provider_load_ms",
                "clinical_llm_prompt_eval_ms",
                "clinical_llm_token_eval_ms",
                "clinical_llm_unattributed_http_ms",
            },
        )
        self.assertTrue(
            all(
                isinstance(value, (int, float)) and value >= 0
                for key, value in result["telemetry"].items()
                if key not in {
                    "translation_batches",
                    "translation_retry_reasons",
                    "clinical_llm_fact_recovery_reasons",
                    "clinical_llm_validation_failure_reasons",
                }
            )
        )
        self.assertEqual(
            result["telemetry"]["translation_batches"],
            [
                {
                    "batch_index": 0,
                    "target_segment_count": 1,
                    "context_segment_count": 1,
                    "request_count": 1,
                    "retry_split_count": 0,
                    "partial_retry_count": 0,
                    "preserved_segment_count": 0,
                    "retry_reasons": {},
                    "rate_limit_count": 0,
                    "failed_segment_count": 0,
                    "elapsed_ms": 140.0,
                }
            ],
        )
        self.assertEqual(result["telemetry"]["translation_provider_calls"], 1)
        self.assertEqual(result["telemetry"]["umls_ms"], 30.0)
        self.assertEqual(result["telemetry"]["umls_model_load_ms"], 50.0)
        self.assertEqual(result["telemetry"]["umls_linking_ms"], 12.0)
        self.assertEqual(result["telemetry"]["umls_worker_batch_count"], 1)
        self.assertEqual(
            result["telemetry"]["umls_worker_cold_start_batch_count"],
            1,
        )
        self.assertEqual(result["telemetry"]["translation_http_ms"], 140.0)
        self.assertEqual(result["telemetry"]["translation_provider_ms"], 120.0)
        self.assertEqual(
            result["telemetry"]["translation_unattributed_http_ms"],
            20.0,
        )
        self.assertEqual(result["telemetry"]["vector_drug_terms_ms"], 12.5)
        self.assertEqual(
            result["telemetry"]["vector_drug_terms_statement_count"],
            2,
        )
        self.assertEqual(
            result["telemetry"]["vector_emergency_terms_ms"],
            7.5,
        )
        self.assertEqual(
            result["telemetry"]["vector_emergency_terms_statement_count"],
            1,
        )
        self.assertEqual(result["telemetry"]["exact_search_query_count"], 3)
        self.assertEqual(result["telemetry"]["vector_fallback_query_count"], 1)
        self.assertEqual(result["telemetry"]["vector_drug_terms_batch_count"], 1)
        self.assertEqual(result["telemetry"]["vector_drug_terms_query_count"], 1)
        self.assertEqual(result["telemetry"]["vector_drug_terms_candidate_count"], 2)
        self.assertEqual(result["telemetry"]["vector_drug_terms_ingredient_ms"], 6.0)
        self.assertEqual(
            result["telemetry"]["vector_drug_terms_product_result_count"],
            1,
        )

    def test_compact_comparison_is_opt_in_and_preserves_v2_draft(self):
        extractor = _SyntheticClinicalExtractor()
        default_runtime = create_clinical_runtime(
            retriever=_EmptyRetriever(),
            clinical_extractor=extractor,
            query_expander=_SyntheticQueryExpander(),
            compact_v3_mode="off",
        )
        payload = {
            "language": "ko",
            "segments": [{
                "id": "seg_0001",
                "start": 0.0,
                "end": 2.0,
                "text": "배가 아파요",
            }],
        }

        default_result = default_runtime.generate_draft(payload)

        self.assertNotIn("compact_v3_comparison", default_result)
        self.assertEqual(extractor.compact_compare_calls, 0)

        compare_runtime = create_clinical_runtime(
            retriever=_EmptyRetriever(),
            clinical_extractor=extractor,
            query_expander=_SyntheticQueryExpander(),
            compact_v3_compare=True,
        )
        compared = compare_runtime.generate_draft(payload)

        self.assertEqual(extractor.compact_compare_calls, 1)
        self.assertEqual(
            compared["compact_v3_comparison"]["status"],
            "completed",
        )
        self.assertTrue(
            compared["compact_v3_comparison"]["fields"]["chief_complaint"][
                "matches"
            ]
        )
        self.assertNotIn(
            "past_history",
            compared["compact_v3_comparison"]["mismatch_field_ids"],
        )
        self.assertIn(
            "review_of_systems",
            compared["compact_v3_comparison"]["evidence_mismatch_field_ids"],
        )
        self.assertEqual(
            compared["compact_v3_comparison"]["fields"]["chief_complaint"][
                "comparison_class"
            ],
            "EXACT_MATCH",
        )
        self.assertEqual(compared["draft"], default_result["draft"])
        self.assertEqual(compared["processing_status"], default_result["processing_status"])
        self.assertEqual(compared["errors"], default_result["errors"])

    def test_compact_comparison_failure_is_isolated_from_v2(self):
        class FailingExtractor(_SyntheticClinicalExtractor):
            def compare_compact_record(self, payload, candidate_snapshots):
                raise RuntimeError("sensitive model preview must not escape")

        runtime = create_clinical_runtime(
            retriever=_EmptyRetriever(),
            clinical_extractor=FailingExtractor(),
            query_expander=_SyntheticQueryExpander(),
            compact_v3_compare=True,
        )

        result = runtime.generate_draft(
            {
                "language": "ko",
                "segments": [{
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 2.0,
                    "text": "배가 아파요",
                }],
            }
        )

        self.assertEqual(result["processing_status"], "completed")
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["draft"]["fields"]["chief_complaint"]["value"], "Abdominal pain.")
        comparison = result["compact_v3_comparison"]
        self.assertEqual(comparison["status"], "unavailable")
        self.assertEqual(comparison["error_code"], "RuntimeError")
        self.assertNotIn("sensitive", comparison["detail"])

    def test_compact_primary_skips_legacy_extraction_and_projects_v2_ui(self):
        extractor = _SyntheticClinicalExtractor()
        runtime = create_clinical_runtime(
            retriever=_EmptyRetriever(),
            clinical_extractor=extractor,
            query_expander=_SyntheticQueryExpander(),
            compact_v3_mode="primary",
        )

        result = runtime.generate_draft(
            {
                "language": "ko",
                "segments": [{
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 2.0,
                    "text": "배가 아파요",
                }],
            }
        )

        self.assertEqual(extractor.extract_calls, 0)
        self.assertEqual(extractor.compact_compare_calls, 1)
        self.assertEqual(result["processing_status"], "completed")
        self.assertEqual(
            result["draft"]["fields"]["chief_complaint"]["value"],
            "Abdominal pain.",
        )
        self.assertEqual(
            result["draft"]["fields"]["chief_complaint"]["information_status"],
            "PRESENT",
        )
        self.assertEqual(
            result["draft"]["fields"]["chief_complaint"]["evidence"][0][
                "segment_id"
            ],
            "seg_0001",
        )
        self.assertEqual(
            result["draft"]["fields"]["past_history"]["information_status"],
            "NOT_ASSESSED",
        )
        self.assertEqual(result["compact_v3_primary"]["status"], "completed")
        self.assertEqual(result["compact_v3_primary"]["validation"]["status"], "PASS")
        self.assertEqual(
            result["audit"]["references"]["compact_record_path"],
            "$.compact_v3_primary.record",
        )
        self.assertEqual(result["telemetry"]["clinical_llm_provider_calls"], 2)
        self.assertEqual(result["telemetry"]["clinical_llm_fact_chunk_count"], 3)
        self.assertEqual(
            result["telemetry"]["clinical_llm_fact_chunk_worker_count"],
            3,
        )
        self.assertEqual(result["telemetry"]["clinical_llm_repair_count"], 1)
        self.assertEqual(result["telemetry"]["clinical_llm_fact_recovery_count"], 1)
        self.assertEqual(
            result["telemetry"]["clinical_llm_fact_recovery_reasons"],
            ["fact[2]: TEXT_FACT_WITHOUT_CANDIDATE_REF_DOWNGRADED"],
        )
        self.assertEqual(
            result["telemetry"]["clinical_llm_fact_targeted_retry_count"], 1
        )
        self.assertEqual(result["telemetry"]["clinical_llm_fact_preserved_count"], 7)
        self.assertEqual(
            result["telemetry"]["clinical_llm_field_reference_retry_count"], 1
        )
        self.assertEqual(
            result["telemetry"]["clinical_llm_field_preserved_count"], 5
        )
        self.assertEqual(result["telemetry"]["clinical_llm_network_retries"], 1)
        self.assertEqual(result["telemetry"]["clinical_llm_http_ms"], 140.0)
        self.assertEqual(result["telemetry"]["clinical_llm_provider_ms"], 120.0)
        self.assertEqual(
            result["telemetry"]["clinical_llm_unattributed_http_ms"],
            20.0,
        )

    def test_lean_primary_projects_sparse_fields_without_legacy_generation_status(self):
        class LeanExtractor(_SyntheticClinicalExtractor):
            def generate_compact_record_lean(self, payload, candidate_snapshots):
                return {
                    "prompt_version": "clinical-record-compact-v3.1-lean-v1",
                    "record": {
                        "schema_version": "clinical-record-compact-v3.1",
                        "facts": {"f1": {
                            "type": "NARRATIVE",
                            "text": "배가 아파요",
                            "assertion": "PRESENT",
                            "segments": ["seg_0001"],
                        }},
                        "fields": {"chief_complaint": {
                            "text": "Abdominal pain",
                            "fact_refs": ["f1"],
                        }},
                    },
                    "validation": {
                        "schema_version": "clinical-record-compact-v3.1-validation-v1",
                        "status": "PASS",
                        "processing_status": "completed",
                        "issues": [],
                        "field_statuses": {"chief_complaint": "PASS"},
                    },
                    "generation": {
                        "contract_version": "clinical-record-compact-v3.1",
                        "generation_route": "single",
                        "llm_call_count": 1,
                    },
                }

        extractor = LeanExtractor()
        runtime = create_clinical_runtime(
            retriever=_EmptyRetriever(),
            clinical_extractor=extractor,
            query_expander=_SyntheticQueryExpander(),
            compact_v3_mode="lean_primary",
        )

        result = runtime.generate_draft({
            "language": "ko",
            "segments": [{
                "id": "seg_0001",
                "start": 0.0,
                "end": 2.0,
                "text": "배가 아파요",
            }],
        })

        self.assertEqual(result["processing_status"], "completed")
        self.assertEqual(
            result["draft"]["fields"]["chief_complaint"]["value"],
            "Abdominal pain",
        )
        self.assertEqual(
            result["draft"]["fields"]["past_history"]["information_status"],
            "NOT_ASSESSED",
        )
        self.assertEqual(result["telemetry"]["contract_version"], "clinical-record-compact-v3.1")
        self.assertNotIn("candidate_snapshots", result["compact_v3_primary"])

    def test_invalid_compact_mode_is_rejected_before_generation(self):
        with self.assertRaisesRegex(ValueError, "off, compare, primary, legacy"):
            create_clinical_runtime(
                retriever=_EmptyRetriever(),
                clinical_extractor=_SyntheticClinicalExtractor(),
                compact_v3_mode="unsafe",
            )

    def test_compact_primary_block_is_propagated_without_erasing_text(self):
        class BlockedCompactExtractor(_SyntheticClinicalExtractor):
            def compare_compact_record(self, payload, candidate_snapshots):
                result = super().compare_compact_record(
                    payload,
                    candidate_snapshots,
                )
                result["validation"] = {
                    "schema_version": "clinical-record-compact-validation-v1",
                    "status": "BLOCK",
                    "processing_status": "completed",
                    "issues": [{
                        "issue_code": "UNSUPPORTED_FACT_REFERENCE",
                        "rule_id": "G01",
                        "severity": "BLOCK",
                        "message": "generated field has unsupported evidence",
                        "field_ids": ["chief_complaint"],
                    }],
                    "field_statuses": {"chief_complaint": "BLOCK"},
                }
                return result

        runtime = create_clinical_runtime(
            retriever=_EmptyRetriever(),
            clinical_extractor=BlockedCompactExtractor(),
            query_expander=_SyntheticQueryExpander(),
            compact_v3_mode="primary",
        )

        result = runtime.generate_draft(
            {
                "language": "ko",
                "segments": [{
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 2.0,
                    "text": "배가 아파요",
                }],
            }
        )

        self.assertEqual(
            result["draft"]["fields"]["chief_complaint"]["value"],
            "Abdominal pain.",
        )
        self.assertEqual(
            result["draft"]["fields"]["chief_complaint"]["information_status"],
            "UNCERTAIN",
        )
        self.assertEqual(result["validation"]["status"], "BLOCK")
        self.assertTrue(
            any(
                issue.get("compact_issue_code") == "UNSUPPORTED_FACT_REFERENCE"
                for issue in result["validation"]["issues"]
            )
        )


if __name__ == "__main__":
    unittest.main()
