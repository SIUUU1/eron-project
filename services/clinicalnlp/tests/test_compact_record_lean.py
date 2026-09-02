import copy
import threading
import unittest

from clinicalnlp_api3.clinical_llm import ClinicalLlmLengthLimit
from clinicalnlp_api3.compact_primary import project_compact_primary_draft
from clinicalnlp_api3.compact_record_lean import (
    SCHEMA_VERSION,
    fact_chunk_response_format,
    lean_record_response_format,
    minimal_candidate_projection,
    validate_lean_record,
)
from clinicalnlp_api3.record_extractor import LlamaServerClinicalExtractor


def _segments(count):
    return [
        {
            "id": f"seg_{index:04d}",
            "start": float(index),
            "end": float(index + 1),
            "raw_text": f"증상 {index}",
            "translated_text_en": f"symptom {index}",
        }
        for index in range(1, count + 1)
    ]


class _LeanClient:
    def __init__(
        self,
        *,
        length_on_first=False,
        fail_segment_id=None,
        measurement=False,
        fail_combined_fields=False,
        split_fact_chunks_over=None,
    ):
        self.length_on_first = length_on_first
        self.fail_segment_id = fail_segment_id
        self.measurement = measurement
        self.fail_combined_fields = fail_combined_fields
        self.split_fact_chunks_over = split_fact_chunks_over
        self._combined_fields_failed = False
        self.calls = []
        self._lock = threading.Lock()

    def generate_json(self, *, system_prompt, user_payload, response_format, output_label):
        with self._lock:
            call_index = len(self.calls)
            self.calls.append({
                "payload": copy.deepcopy(user_payload),
                "name": response_format["json_schema"]["name"],
                "system_prompt": system_prompt,
            })
        if self.length_on_first and call_index == 0:
            raise ClinicalLlmLengthLimit("synthetic length")
        name = response_format["json_schema"]["name"]
        if name == "clinical_record_compact_v3_1":
            segment_id = user_payload["segments"][0]["id"]
            return {
                "schema_version": SCHEMA_VERSION,
                "facts": {"f1": {
                    "type": "NARRATIVE",
                    "text": "supported symptom",
                    "assertion": "PRESENT",
                    "segments": [segment_id],
                }},
                "fields": {"chief_complaint": {
                    "text": "Cough",
                    "fact_refs": ["f1"],
                }},
            }
        if name == "clinical_record_compact_facts_v1":
            if self.fail_segment_id in user_payload["owned_segment_ids"]:
                raise RuntimeError("synthetic chunk failure")
            if (
                isinstance(self.split_fact_chunks_over, int)
                and len(user_payload["owned_segment_ids"])
                > self.split_fact_chunks_over
            ):
                raise ClinicalLlmLengthLimit("synthetic chunk length")
            segment_id = user_payload["owned_segment_ids"][0]
            if self.measurement:
                return {
                    "schema_version": "clinical-record-compact-facts-v1",
                    "facts": {"f1": {
                        "type": "MEASUREMENT",
                        "values": {"kind": "BP", "value": "138/82", "unit": "mmHg"},
                        "assertion": "PRESENT",
                        "segments": [segment_id],
                        "fact_type": "EXAM",
                    }},
                }
            return {
                "schema_version": "clinical-record-compact-facts-v1",
                "facts": {"f1": {
                    "type": "NARRATIVE",
                    "text": f"fact {segment_id}",
                    "assertion": "PRESENT",
                    "segments": [segment_id],
                }},
            }
        requested_fields = user_payload.get("requested_fields", [])
        if (
            self.fail_combined_fields
            and len(requested_fields) == 12
            and not self._combined_fields_failed
        ):
            self._combined_fields_failed = True
            raise RuntimeError("synthetic all-fields failure")
        first_fact = next(iter(user_payload["facts"]), None)
        if self.measurement:
            fields = ({
                "physical_examination": {
                    "text": "Vital signs: BP 138/82 mmHg",
                    "fact_refs": [first_fact],
                }
            } if first_fact and "physical_examination" in requested_fields else {})
        else:
            fields = ({
                "chief_complaint": {
                    "text": "Cough",
                    "fact_refs": [first_fact],
                }
            } if first_fact and "chief_complaint" in requested_fields else {})
        return {
            "schema_version": "clinical-record-compact-fields-v1",
            "fields": fields,
        }


class CompactRecordLeanContractTests(unittest.TestCase):
    def test_schema_is_sparse_and_has_no_dynamic_reference_enums(self):
        response_format = lean_record_response_format()
        schema = response_format["json_schema"]["schema"]

        self.assertEqual(schema["properties"]["schema_version"]["const"], SCHEMA_VERSION)
        self.assertNotIn("required", schema["properties"]["fields"])
        schema_text = str(schema)
        self.assertNotIn("allOf", schema_text)
        self.assertNotIn("candidate_ref': {'type': 'string', 'enum'", schema_text)
        self.assertEqual(
            fact_chunk_response_format()["json_schema"]["name"],
            "clinical_record_compact_facts_v1",
        )

    def test_candidate_projection_excludes_audit_and_score_data(self):
        projection = minimal_candidate_projection({"cr_one": {
            "segment_id": "seg_0001",
            "source_span": "코프",
            "canonical": "Cough",
            "semantic_types": ["T184"],
            "retrieval_source": "umls",
            "retrieval_score": 0.99,
            "concept_id": "C0010200",
            "versions": {"dictionary": "test"},
        }})

        self.assertEqual(set(projection[0]), {
            "candidate_ref", "segment_id", "surface", "canonical",
            "semantic_types", "source",
        })

    def test_missing_fields_project_to_empty_not_assessed(self):
        record = {"schema_version": SCHEMA_VERSION, "facts": {}, "fields": {}}
        validation = validate_lean_record(
            record,
            segment_ids=["seg_0001"],
            candidate_snapshots={},
        )
        draft = project_compact_primary_draft(
            record,
            validation,
            {"segments": _segments(1)},
        )

        self.assertEqual(draft["fields"]["chief"]["value"], "")
        self.assertEqual(draft["fields"]["chief"]["status"], "empty")
        self.assertEqual(draft["fields"]["chief"]["information_status"], "NOT_ASSESSED")

    def test_backend_rejects_wrong_lean_version_even_after_model_validation(self):
        validation = validate_lean_record(
            {"schema_version": "wrong", "facts": {}, "fields": {}},
            segment_ids=[],
            candidate_snapshots={},
        )

        self.assertEqual(validation["status"], "BLOCK")
        self.assertIn(
            "INVALID_LEAN_CONTRACT",
            {issue["issue_code"] for issue in validation["issues"]},
        )

    def test_short_input_uses_one_lean_call(self):
        client = _LeanClient()
        extractor = LlamaServerClinicalExtractor("http://unused", llm_client=client)

        result = extractor.generate_compact_record_lean({"segments": _segments(2)}, {})

        self.assertEqual(result["record"]["schema_version"], SCHEMA_VERSION)
        self.assertEqual(result["generation"]["generation_route"], "single")
        self.assertEqual(result["generation"]["llm_call_count"], 1)
        self.assertEqual(len(client.calls), 1)
        self.assertIn(
            "no separate structured vital_signs field",
            client.calls[0]["system_prompt"],
        )

    def test_length_switches_once_to_chunked_facts_and_fields(self):
        client = _LeanClient(length_on_first=True)
        extractor = LlamaServerClinicalExtractor("http://unused", llm_client=client)

        result = extractor.generate_compact_record_lean({"segments": _segments(3)}, {})

        self.assertEqual(result["generation"]["generation_route"], "chunked")
        self.assertEqual(result["generation"]["length_fallback_count"], 1)
        self.assertLessEqual(result["generation"]["llm_call_count"], 14)
        self.assertTrue(result["record"]["facts"])
        self.assertIn("chief_complaint", result["record"]["fields"])

    def test_failed_chunk_preserves_successful_facts_and_marks_partial(self):
        client = _LeanClient(fail_segment_id="seg_0017")
        extractor = LlamaServerClinicalExtractor("http://unused", llm_client=client)

        result = extractor.generate_compact_record_lean({"segments": _segments(17)}, {})

        self.assertEqual(result["generation"]["generation_route"], "chunked")
        self.assertEqual(result["validation"]["processing_status"], "partial")
        self.assertTrue(result["record"]["facts"])
        failed_issue = next(
            issue
            for issue in result["validation"]["issues"]
            if issue.get("issue_code") == "CHUNK_GENERATION_FAILED"
        )
        self.assertEqual(failed_issue["segment_ids"], ["seg_0017"])
        field_payload = next(
            call["payload"]
            for call in client.calls
            if call["name"] == "clinical_record_compact_fields_v1"
        )
        self.assertNotIn(
            "seg_0017",
            {segment["id"] for segment in field_payload["segments"]},
        )

    def test_vital_measurement_is_assigned_to_physical_examination(self):
        client = _LeanClient(length_on_first=True, measurement=True)
        extractor = LlamaServerClinicalExtractor("http://unused", llm_client=client)

        result = extractor.generate_compact_record_lean({"segments": _segments(3)}, {})

        physical = result["record"]["fields"]["physical_examination"]
        self.assertEqual(physical["text"], "Vital signs: BP 138/82 mmHg")
        self.assertEqual(physical["fact_refs"], ["c01_f001"])
        self.assertFalse(any(
            issue.get("issue_code") in {
                "UNASSIGNED_FACT",
                "UNRESOLVED_FACT_TYPE",
                "FACT_TYPE_FIELD_MISMATCH",
            }
            for issue in result["validation"]["issues"]
        ))
        field_call = next(
            call for call in client.calls
            if call["name"] == "clinical_record_compact_fields_v1"
        )
        fact_call = next(
            call for call in client.calls
            if call["name"] == "clinical_record_compact_facts_v1"
        )
        self.assertIn("no separate vital_signs field", fact_call["system_prompt"])
        self.assertIn("no separate vital_signs field", field_call["system_prompt"])

    def test_all_fields_failure_retries_fixed_field_groups(self):
        client = _LeanClient(fail_combined_fields=True)
        extractor = LlamaServerClinicalExtractor("http://unused", llm_client=client)

        result = extractor.generate_compact_record_lean({"segments": _segments(128)}, {})

        self.assertEqual(result["generation"]["fact_chunk_count"], 8)
        self.assertEqual(result["generation"]["field_group_call_count"], 3)
        self.assertIn("chief_complaint", result["record"]["fields"])
        self.assertNotIn("FIELD_GENERATION_FAILED", {
            issue.get("issue_code") for issue in result["validation"]["issues"]
        })

    def test_split_chunk_audit_ids_match_fact_id_map(self):
        client = _LeanClient(split_fact_chunks_over=8)
        extractor = LlamaServerClinicalExtractor("http://unused", llm_client=client)

        result = extractor.generate_compact_record_lean({"segments": _segments(17)}, {})

        completed_audit_ids = {
            item["chunk_id"]
            for item in result["audit"]["chunks"]
            if item["status"] == "completed"
        }
        mapped_chunk_ids = {
            item["chunk_id"] for item in result["audit"]["fact_id_map"]
        }
        self.assertEqual(completed_audit_ids, mapped_chunk_ids)
        self.assertEqual(completed_audit_ids, {"chunk_01", "chunk_02", "chunk_03"})

    def test_dangling_supersedes_reference_is_blocked(self):
        record = {
            "schema_version": SCHEMA_VERSION,
            "facts": {"f1": {
                "type": "NARRATIVE",
                "text": "corrected symptom",
                "assertion": "PRESENT",
                "segments": ["seg_0001"],
                "supersedes_fact_id": "missing_fact",
            }},
            "fields": {"chief_complaint": {
                "text": "Corrected symptom",
                "fact_refs": ["f1"],
            }},
        }

        validation = validate_lean_record(
            record,
            segment_ids=["seg_0001"],
            candidate_snapshots={},
        )

        self.assertEqual(validation["status"], "BLOCK")
        issue = next(
            item for item in validation["issues"]
            if item.get("issue_code") == "INVALID_SUPERSEDES_FACT_REFERENCE"
        )
        self.assertEqual(issue["fact_id"], "f1")
        self.assertEqual(issue["field_ids"], ["chief_complaint"])


if __name__ == "__main__":
    unittest.main()
