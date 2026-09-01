import copy
import unittest

from clinicalnlp_api3.candidate_snapshot import (
    seal_candidate_snapshot,
    verify_candidate_snapshot,
)
from clinicalnlp_api3.compact_record_v3 import (
    CANONICAL_FIELD_IDS,
    SCHEMA_VERSION,
    compact_record_response_format,
    validate_compact_record,
)


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


if __name__ == "__main__":
    unittest.main()
