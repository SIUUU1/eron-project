import copy
import unittest

from clinicalnlp_api3.candidate_snapshot import (
    seal_candidate_snapshot,
    snapshots_from_api3_document,
    verify_candidate_snapshot,
)
from clinicalnlp_api3.compact_record_v3 import (
    CANONICAL_FIELD_IDS,
    SCHEMA_VERSION,
    compact_record_response_format,
    validate_compact_record,
)
from clinicalnlp_api3.record_extractor import LlamaServerClinicalExtractor


def candidate_snapshot(
    *,
    segment_id="seg_0001",
    source_span="코프",
    candidate_id="umls:C0010200",
    concept_id="C0010200",
    canonical="Cough",
):
    return seal_candidate_snapshot(
        {
            "request_id": "req_001",
            "query_id": f"query:{segment_id}:{source_span}",
            "segment_id": segment_id,
            "source_span": source_span,
            "source_start": 0,
            "source_end": len(source_span),
            "translated_query": canonical.casefold(),
            "candidate_id": candidate_id,
            "canonical": canonical,
            "concept_id": concept_id,
            "semantic_types": ["T184"],
            "retrieval_source": "UMLS",
            "retrieval_score": 0.97,
            "rank": 1,
            "versions": {
                "umls": "2022AB",
                "terminology": "medical-terms-v1",
                "vector": "medical-vector-v1",
            },
            "created_at": "2026-09-01T00:00:00+00:00",
        }
    )


def empty_fields():
    return {
        field_id: {
            "generation_status": "NOT_MENTIONED",
            "text": None,
            "fact_refs": [],
        }
        for field_id in CANONICAL_FIELD_IDS
    }


def compact_document(*, facts=None, fields=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "facts": facts or {},
        "fields": fields or empty_fields(),
    }


class CandidateSnapshotTests(unittest.TestCase):
    def test_snapshot_is_tamper_evident_and_reproducible(self):
        first = candidate_snapshot()
        second = candidate_snapshot()

        self.assertEqual(first, second)
        self.assertTrue(verify_candidate_snapshot(first))
        self.assertTrue(first["candidate_ref"].startswith("cr_"))

        changed = copy.deepcopy(first)
        changed["canonical"] = "Cough variant asthma"
        self.assertFalse(verify_candidate_snapshot(changed))

    def test_api3_candidates_require_exact_offsets_and_a_version(self):
        document = {
            "segments": [{
                "id": "seg_0001",
                "raw_text": "코프가 있어요",
                "annotations": [{
                    "source_span": {
                        "text": "코프",
                        "start_char": 0,
                        "end_char": 2,
                    },
                    "search_terms_en": ["cough"],
                    "candidates": [{
                        "entity_id": "umls:C0010200",
                        "canonical_en": "Cough",
                        "match_type": "umls_dictionary_search",
                        "retrieval_score": 0.97,
                        "dictionary_version": "umls-2022AB",
                        "provenance": {
                            "cui": "C0010200",
                            "semantic_types": ["T184"],
                        },
                    }, {
                        "entity_id": "unversioned",
                        "canonical_en": "Cough",
                        "match_type": "vector",
                        "retrieval_score": 0.80,
                    }],
                }],
            }],
        }

        snapshots = snapshots_from_api3_document(
            document,
            request_id="req_001",
            created_at="2026-09-01T00:00:00+00:00",
        )

        self.assertEqual(len(snapshots), 1)
        snapshot = next(iter(snapshots.values()))
        self.assertTrue(verify_candidate_snapshot(snapshot))
        self.assertEqual(snapshot["source_span"], "코프")
        self.assertEqual(snapshot["versions"], {"dictionary": "umls-2022AB"})

        document["segments"][0]["annotations"][0]["source_span"]["text"] = "오프"
        self.assertEqual(
            snapshots_from_api3_document(document, request_id="req_002"),
            {},
        )


class CompactRecordContractTests(unittest.TestCase):
    def test_model_contract_binds_segments_candidates_and_all_fields(self):
        snapshot = candidate_snapshot()
        response_format = compact_record_response_format(
            ["seg_0001"], [snapshot["candidate_ref"]]
        )
        schema = response_format["json_schema"]["schema"]

        self.assertEqual(response_format["json_schema"]["name"], "clinical_record_compact_v3")
        self.assertEqual(schema["properties"]["schema_version"]["const"], SCHEMA_VERSION)
        self.assertEqual(
            set(schema["properties"]["fields"]["required"]),
            set(CANONICAL_FIELD_IDS),
        )
        fact_variants = schema["properties"]["facts"]["additionalProperties"]["oneOf"]
        matched = next(
            item for item in fact_variants if item["properties"]["type"]["const"] == "MATCHED_TERM"
        )
        self.assertEqual(
            matched["properties"]["candidate_ref"]["enum"],
            [snapshot["candidate_ref"]],
        )
        self.assertEqual(
            matched["properties"]["segments"]["items"]["enum"],
            ["seg_0001"],
        )

    def test_model_contract_disallows_matched_terms_without_snapshots(self):
        response_format = compact_record_response_format(["seg_0001"], [])
        variants = response_format["json_schema"]["schema"]["properties"][
            "facts"
        ]["additionalProperties"]["oneOf"]

        self.assertNotIn(
            "MATCHED_TERM",
            {item["properties"]["type"]["const"] for item in variants},
        )

    def test_model_contract_binds_each_candidate_to_its_snapshot_segment(self):
        snapshot = candidate_snapshot()
        response_format = compact_record_response_format(
            ["seg_0001", "seg_0002"],
            {snapshot["candidate_ref"]: snapshot},
        )
        variants = response_format["json_schema"]["schema"]["properties"][
            "facts"
        ]["additionalProperties"]["oneOf"]
        matched = next(
            item
            for item in variants
            if item["properties"]["type"]["const"] == "MATCHED_TERM"
        )

        self.assertEqual(
            matched["properties"]["candidate_ref"]["enum"],
            [snapshot["candidate_ref"]],
        )
        self.assertEqual(
            matched["allOf"],
            [{
                "if": {
                    "required": ["candidate_ref"],
                    "properties": {
                        "candidate_ref": {"const": snapshot["candidate_ref"]}
                    },
                },
                "then": {
                    "properties": {
                        "segments": {"contains": {"const": "seg_0001"}}
                    }
                },
            }],
        )

    def test_valid_record_passes_without_rewriting_the_draft(self):
        snapshot = candidate_snapshot()
        facts = {
            "f1": {
                "type": "MATCHED_TERM",
                "candidate_ref": snapshot["candidate_ref"],
                "assertion": "PRESENT",
                "segments": ["seg_0001"],
            }
        }
        fields = empty_fields()
        fields["chief_complaint"] = {
            "generation_status": "GENERATED",
            "text": "Cough",
            "fact_refs": ["f1"],
        }
        document = compact_document(facts=facts, fields=fields)
        before = copy.deepcopy(document)

        result = validate_compact_record(
            document,
            segment_ids=["seg_0001"],
            candidate_snapshots={snapshot["candidate_ref"]: snapshot},
        )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["processing_status"], "completed")
        self.assertEqual(result["field_statuses"]["chief_complaint"], "PASS")
        self.assertEqual(result["issues"], [])
        self.assertEqual(document, before)

    def test_unmatched_term_is_preserved_as_a_first_class_fact(self):
        facts = {
            "f1": {
                "type": "UNMATCHED_TERM",
                "text": "리네일러",
                "review_code": "NO_MATCH",
                "assertion": "PRESENT",
                "segments": ["seg_0004"],
            }
        }
        fields = empty_fields()
        fields["medications"] = {
            "generation_status": "GENERATED",
            "text": "리네일러",
            "fact_refs": ["f1"],
        }
        result = validate_compact_record(
            compact_document(facts=facts, fields=fields),
            segment_ids=["seg_0004"],
            candidate_snapshots={},
        )

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["fact_statuses"]["f1"], "PASS")

    def test_invalid_candidate_only_escalates_its_fact_and_referencing_field(self):
        valid = candidate_snapshot()
        facts = {
            "f1": {
                "type": "MATCHED_TERM",
                "candidate_ref": valid["candidate_ref"],
                "assertion": "PRESENT",
                "segments": ["seg_0001"],
            },
            "f2": {
                "type": "MATCHED_TERM",
                "candidate_ref": "cr_missing",
                "assertion": "PRESENT",
                "segments": ["seg_0001"],
            },
        }
        fields = empty_fields()
        fields["chief_complaint"] = {
            "generation_status": "GENERATED",
            "text": "Cough, Dyspnea",
            "fact_refs": ["f1", "f2"],
        }
        result = validate_compact_record(
            compact_document(facts=facts, fields=fields),
            segment_ids=["seg_0001"],
            candidate_snapshots={valid["candidate_ref"]: valid},
        )

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["fact_statuses"]["f1"], "PASS")
        self.assertEqual(result["fact_statuses"]["f2"], "REVIEW_REQUIRED")
        self.assertEqual(
            result["field_statuses"]["chief_complaint"], "REVIEW_REQUIRED"
        )
        issue = next(
            item for item in result["issues"] if item["issue_code"] == "INVALID_CANDIDATE_REF"
        )
        self.assertEqual(issue["field_ids"], ["chief_complaint"])

    def test_unknown_segment_blocks_but_does_not_delete_text(self):
        facts = {
            "f1": {
                "type": "NARRATIVE",
                "text": "4일 전부터 dyspnea 증가함",
                "assertion": "PRESENT",
                "segments": ["seg_missing"],
            }
        }
        fields = empty_fields()
        fields["history_of_present_illness"] = {
            "generation_status": "GENERATED",
            "text": "4일 전부터 dyspnea 증가함",
            "fact_refs": ["f1"],
        }
        document = compact_document(facts=facts, fields=fields)

        result = validate_compact_record(
            document,
            segment_ids=["seg_0001"],
            candidate_snapshots={},
        )

        self.assertEqual(result["status"], "BLOCK")
        self.assertEqual(
            result["field_statuses"]["history_of_present_illness"], "BLOCK"
        )
        self.assertEqual(
            document["fields"]["history_of_present_illness"]["text"],
            "4일 전부터 dyspnea 증가함",
        )

    def test_failed_field_is_distinct_from_not_mentioned_and_keeps_partial_output(self):
        fields = empty_fields()
        fields["past_history"] = {
            "generation_status": "FAILED",
            "text": None,
            "fact_refs": [],
            "error_code": "PAST_HISTORY_GENERATION_FAILED",
        }
        result = validate_compact_record(
            compact_document(fields=fields),
            segment_ids=[],
            candidate_snapshots={},
        )

        self.assertEqual(result["processing_status"], "partial")
        self.assertEqual(result["field_statuses"]["past_history"], "REVIEW_REQUIRED")
        self.assertEqual(result["field_statuses"]["drug_allergy"], "PASS")
        self.assertEqual(
            next(
                item
                for item in result["issues"]
                if item["issue_code"] == "FIELD_GENERATION_FAILED"
            )["field_ids"],
            ["past_history"],
        )

    def test_generated_text_without_valid_fact_reference_is_blocked(self):
        fields = empty_fields()
        fields["chief_complaint"] = {
            "generation_status": "GENERATED",
            "text": "Cough",
            "fact_refs": [],
        }
        result = validate_compact_record(
            compact_document(fields=fields),
            segment_ids=["seg_0001"],
            candidate_snapshots={},
        )

        self.assertEqual(result["status"], "BLOCK")
        self.assertIn(
            "UNSUPPORTED_FACT_REFERENCE",
            {item["issue_code"] for item in result["issues"]},
        )

    def test_unreferenced_invalid_fact_still_blocks_document(self):
        document = compact_document(
            facts={
                "f_orphan": {
                    "type": "NARRATIVE",
                    "text": "근거 없는 문장",
                    "assertion": "PRESENT",
                    "segments": ["seg_missing"],
                }
            }
        )
        result = validate_compact_record(
            document,
            segment_ids=["seg_0001"],
            candidate_snapshots={},
        )

        self.assertEqual(result["status"], "BLOCK")
        self.assertEqual(result["field_statuses"]["chief_complaint"], "PASS")

    def test_unknown_field_blocks_document_without_changing_known_fields(self):
        fields = empty_fields()
        fields["invented_field"] = {
            "generation_status": "NOT_MENTIONED",
            "text": None,
            "fact_refs": [],
        }
        result = validate_compact_record(
            compact_document(fields=fields),
            segment_ids=[],
            candidate_snapshots={},
        )

        self.assertEqual(result["status"], "BLOCK")
        self.assertEqual(result["field_statuses"]["chief_complaint"], "PASS")

    def test_candidate_snapshot_tampering_blocks_referenced_field(self):
        snapshot = candidate_snapshot()
        tampered = copy.deepcopy(snapshot)
        tampered["canonical"] = "Cough variant asthma"
        facts = {
            "f1": {
                "type": "MATCHED_TERM",
                "candidate_ref": snapshot["candidate_ref"],
                "assertion": "PRESENT",
                "segments": ["seg_0001"],
            }
        }
        fields = empty_fields()
        fields["chief_complaint"] = {
            "generation_status": "GENERATED",
            "text": "Cough",
            "fact_refs": ["f1"],
        }
        result = validate_compact_record(
            compact_document(facts=facts, fields=fields),
            segment_ids=["seg_0001"],
            candidate_snapshots={snapshot["candidate_ref"]: tampered},
        )

        self.assertEqual(result["status"], "BLOCK")
        self.assertIn(
            "INVALID_CANDIDATE_SNAPSHOT",
            {item["issue_code"] for item in result["issues"]},
        )

    def test_unresolved_assertion_conflict_is_reviewed_and_supersedes_resolves_it(self):
        denied = candidate_snapshot(candidate_id="cough-denied")
        present = candidate_snapshot(candidate_id="cough-present")
        facts = {
            "f1": {
                "type": "MATCHED_TERM",
                "candidate_ref": denied["candidate_ref"],
                "assertion": "DENIED",
                "segments": ["seg_0001"],
            },
            "f2": {
                "type": "MATCHED_TERM",
                "candidate_ref": present["candidate_ref"],
                "assertion": "PRESENT",
                "segments": ["seg_0001"],
            },
        }
        fields = empty_fields()
        fields["review_of_systems"] = {
            "generation_status": "GENERATED",
            "text": "Cough 확인 필요",
            "fact_refs": ["f1", "f2"],
        }
        snapshots = {
            denied["candidate_ref"]: denied,
            present["candidate_ref"]: present,
        }

        conflict = validate_compact_record(
            compact_document(facts=facts, fields=fields),
            segment_ids=["seg_0001"],
            candidate_snapshots=snapshots,
        )
        self.assertIn(
            "CONFLICTING_ASSERTION",
            {item["issue_code"] for item in conflict["issues"]},
        )

        resolved_facts = copy.deepcopy(facts)
        resolved_facts["f2"]["supersedes_fact_id"] = "f1"
        resolved = validate_compact_record(
            compact_document(facts=resolved_facts, fields=fields),
            segment_ids=["seg_0001"],
            candidate_snapshots=snapshots,
        )
        self.assertNotIn(
            "CONFLICTING_ASSERTION",
            {item["issue_code"] for item in resolved["issues"]},
        )

    def test_supersedes_cycle_is_blocked_and_cannot_hide_assertion_conflict(self):
        denied = candidate_snapshot(candidate_id="cough-denied")
        present = candidate_snapshot(candidate_id="cough-present")
        facts = {
            "f1": {
                "type": "MATCHED_TERM",
                "candidate_ref": denied["candidate_ref"],
                "assertion": "DENIED",
                "segments": ["seg_0001"],
                "supersedes_fact_id": "f2",
            },
            "f2": {
                "type": "MATCHED_TERM",
                "candidate_ref": present["candidate_ref"],
                "assertion": "PRESENT",
                "segments": ["seg_0001"],
                "supersedes_fact_id": "f1",
            },
        }
        fields = empty_fields()
        fields["review_of_systems"] = {
            "generation_status": "GENERATED",
            "text": "Cough(?)",
            "fact_refs": ["f1", "f2"],
        }
        snapshots = {
            denied["candidate_ref"]: denied,
            present["candidate_ref"]: present,
        }

        result = validate_compact_record(
            compact_document(facts=facts, fields=fields),
            segment_ids=["seg_0001"],
            candidate_snapshots=snapshots,
        )

        self.assertEqual(result["status"], "BLOCK")
        self.assertIn(
            "INVALID_FACT_RELATION",
            {item["issue_code"] for item in result["issues"]},
        )

    def test_missing_or_invalid_field_contract_changes_processing_status(self):
        fields = empty_fields()
        fields.pop("past_history")
        fields["medications"]["generation_status"] = "BROKEN"

        result = validate_compact_record(
            compact_document(fields=fields),
            segment_ids=[],
            candidate_snapshots={},
        )

        self.assertEqual(result["processing_status"], "partial")
        self.assertEqual(result["summary"]["failed_field_count"], 2)

    def test_unknown_fact_type_preserves_high_risk_text_for_review(self):
        facts = {
            "f1": {
                "type": "NARRATIVE",
                "fact_type": "UNKNOWN",
                "text": "복부 CT를 진행하겠습니다",
                "assertion": "PRESENT",
                "segments": ["seg_0001"],
            }
        }
        fields = empty_fields()
        fields["treatment_plan"] = {
            "generation_status": "GENERATED",
            "text": "Diagnostic Workup: Abdomen CT",
            "fact_refs": ["f1"],
        }
        document = compact_document(facts=facts, fields=fields)

        result = validate_compact_record(
            document,
            segment_ids=["seg_0001"],
            candidate_snapshots={},
        )

        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["processing_status"], "completed")
        self.assertEqual(
            result["field_statuses"]["treatment_plan"], "REVIEW_REQUIRED"
        )
        self.assertEqual(
            document["fields"]["treatment_plan"]["text"],
            "Diagnostic Workup: Abdomen CT",
        )

    def test_high_risk_field_rejects_mismatched_fact_type(self):
        facts = {
            "f1": {
                "type": "NARRATIVE",
                "fact_type": "ASSESSMENT",
                "text": "충수염이 의심됩니다",
                "assertion": "UNCERTAIN",
                "segments": ["seg_0001"],
            }
        }
        fields = empty_fields()
        fields["treatment_plan"] = {
            "generation_status": "GENERATED",
            "text": "R/O Acute appendicitis",
            "fact_refs": ["f1"],
        }

        result = validate_compact_record(
            compact_document(facts=facts, fields=fields),
            segment_ids=["seg_0001"],
            candidate_snapshots={},
        )

        self.assertEqual(
            result["field_statuses"]["treatment_plan"], "REVIEW_REQUIRED"
        )
        self.assertIn(
            "FACT_TYPE_FIELD_MISMATCH",
            {item["issue_code"] for item in result["issues"]},
        )


class CompactRecordExtractorTests(unittest.TestCase):
    def test_comparison_uses_compact_contract_and_validates_result(self):
        class Client:
            def __init__(self):
                self.calls = []

            def generate_json(self, **kwargs):
                self.calls.append(kwargs)
                return compact_document()

        client = Client()
        extractor = LlamaServerClinicalExtractor(
            "http://unused.invalid",
            llm_client=client,
        )

        result = extractor.compare_compact_record(
            {
                "segments": [{
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 1.0,
                    "raw_text": "확인된 임상 내용 없음",
                    "translated_text_en": "No clinical content was stated.",
                    "annotations": [],
                }]
            },
            {},
        )

        self.assertEqual(result["validation"]["status"], "PASS")
        self.assertEqual(result["prompt_version"], "clinical-record-compact-v3.2")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            client.calls[0]["response_format"]["json_schema"]["name"],
            "clinical_record_compact_v3",
        )
        self.assertIn("Compact v3 output contract", client.calls[0]["system_prompt"])
        self.assertNotIn("Return JSON only:\n{\n  \"clinical_record\"", client.calls[0]["system_prompt"])


if __name__ == "__main__":
    unittest.main()
