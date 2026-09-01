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
        }


class _SyntheticClinicalExtractor:
    def extract(self, payload):
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


class _SyntheticMedicalQueryResolver:
    def resolve(self, document):
        return QueryResolution(
            mode="umls_primary",
            status="complete",
            policy_version="synthetic-policy-v1",
            telemetry=QueryResolutionTelemetry(
                vector_ms=20.0,
                vector_statement_count=3,
                vector_collection_ms=(
                    ("drug_terms", 12.5),
                    ("emergency_terms", 7.5),
                ),
                vector_collection_statement_counts=(
                    ("drug_terms", 2),
                    ("emergency_terms", 1),
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
                "umls_ms",
                "dictionary_ms",
                "vector_ms",
                "exact_statement_count",
                "vector_statement_count",
                "search_cache_hit_count",
                "vector_drug_terms_ms",
                "vector_drug_terms_statement_count",
                "vector_procedure_terms_ms",
                "vector_procedure_terms_statement_count",
                "vector_anatomy_terms_ms",
                "vector_anatomy_terms_statement_count",
                "vector_emergency_terms_ms",
                "vector_emergency_terms_statement_count",
                "clinical_extraction_ms",
            },
        )
        self.assertTrue(
            all(
                isinstance(value, (int, float)) and value >= 0
                for value in result["telemetry"].values()
            )
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


if __name__ == "__main__":
    unittest.main()
