import copy
import unittest

from clinicalnlp_api3.runtime_validation import validate_clinical_workflow
from clinicalnlp_api3.workflow_contract_v2 import CANONICAL_FIELD_IDS


def workflow_document():
    fields = {
        field_id: {
            "field_id": field_id,
            "value": "",
            "information_status": "NOT_ASSESSED",
            "evidence": [],
        }
        for field_id in CANONICAL_FIELD_IDS
    }
    return {
        "schema_version": "clinical-workflow-v2",
        "processing_status": "completed",
        "record_status": "DRAFT",
        "validation": {"status": "PASS", "issues": []},
        "completed_at": None,
        "api3": {"segments": []},
        "api2": {"clinical_record": {}},
        "candidate_decisions": [],
        "draft": {"fields": fields, "review_items": []},
        "errors": [],
    }


class RuntimeValidationTests(unittest.TestCase):
    def test_g01_blocks_a_clinical_fact_without_conversation_evidence(self):
        document = workflow_document()
        document["api2"]["clinical_record"]["chief_complaint"] = {
            "raw_value": "pneumonia",
            "status": "confirmed",
            "evidence": None,
        }
        field = document["draft"]["fields"]["chief_complaint"]
        field.update(
            {
                "value": "pneumonia",
                "information_status": "PRESENT",
                "evidence": [],
            }
        )

        validation = validate_clinical_workflow(document, policy_index_path=None)

        issue = next(item for item in validation["issues"] if item["rule_id"] == "G01")
        self.assertEqual(validation["status"], "BLOCK")
        self.assertEqual(issue["field_id"], "chief_complaint")
        self.assertEqual(field["value"], "pneumonia")

    def test_unique_text_and_timing_evidence_is_grounded_without_a_segment_id(self):
        document = workflow_document()
        text = "가슴이 아파요"
        document["api3"]["segments"] = [
            {
                "id": "seg_0001",
                "start": 0,
                "end": 2,
                "raw_text": text,
                "corrected_text": text,
                "annotations": [],
            }
        ]
        document["api2"]["clinical_record"]["chief_complaint"] = {
            "raw_value": text,
            "status": "confirmed",
            "evidence": {"text": text, "start": 0, "end": 2},
        }
        document["draft"]["fields"]["chief_complaint"].update(
            {
                "value": text,
                "information_status": "PRESENT",
                "evidence": [
                    {
                        "segment_id": "seg_0001",
                        "text": text,
                        "raw_text": text,
                        "start": 0,
                        "end": 2,
                    }
                ],
            }
        )

        validation = validate_clinical_workflow(document, policy_index_path=None)

        self.assertFalse(
            any(issue["rule_id"] == "G01" for issue in validation["issues"])
        )

    def test_g02_blocks_none_when_the_information_was_not_assessed(self):
        document = workflow_document()
        field = document["draft"]["fields"]["past_history"]
        field.update(
            {
                "value": "없음",
                "information_status": "NONE",
                "evidence": [],
            }
        )

        validation = validate_clinical_workflow(document, policy_index_path=None)

        issue = next(item for item in validation["issues"] if item["rule_id"] == "G02")
        self.assertEqual(issue["field_id"], "past_history")
        self.assertEqual(issue["severity"], "BLOCK")
        self.assertEqual(field["information_status"], "NONE")

    def test_g03_blocks_a_hedged_statement_recorded_as_present(self):
        document = workflow_document()
        text = "아마 당뇨약인 것 같아요"
        document["api3"]["segments"] = [
            {
                "id": "seg_0001",
                "start": 0,
                "end": 2,
                "raw_text": text,
                "corrected_text": text,
                "annotations": [],
            }
        ]
        document["api2"]["clinical_record"]["medications"] = {
            "items": [
                {
                    "raw_value": text,
                    "status": "confirmed",
                    "evidence": {"source_segment_id": "seg_0001"},
                }
            ]
        }
        field = document["draft"]["fields"]["medications"]
        field.update(
            {
                "value": text,
                "information_status": "PRESENT",
                "evidence": [
                    {
                        "segment_id": "seg_0001",
                        "start": 0,
                        "end": 2,
                        "text": text,
                        "raw_text": text,
                    }
                ],
            }
        )

        validation = validate_clinical_workflow(document, policy_index_path=None)

        issue = next(item for item in validation["issues"] if item["rule_id"] == "G03")
        self.assertEqual(issue["field_id"], "medications")
        self.assertEqual(issue["severity"], "BLOCK")
        self.assertEqual(field["information_status"], "PRESENT")

    def test_g04_blocks_a_conflict_that_was_resolved_without_review(self):
        document = workflow_document()
        texts = ["고혈압이 있습니다", "고혈압은 없습니다"]
        document["api3"]["segments"] = [
            {
                "id": f"seg_{index:04d}",
                "start": index - 1,
                "end": index,
                "raw_text": text,
                "corrected_text": text,
                "annotations": [],
            }
            for index, text in enumerate(texts, start=1)
        ]
        document["api2"]["clinical_record"]["past_history"] = {
            "underlying_conditions": [
                {
                    "raw_value": text,
                    "status": "confirmed",
                    "evidence": {"source_segment_id": f"seg_{index:04d}"},
                }
                for index, text in enumerate(texts, start=1)
            ]
        }
        field = document["draft"]["fields"]["past_history"]
        field.update(
            {
                "value": "고혈압",
                "information_status": "PRESENT",
                "evidence": [
                    {
                        "segment_id": f"seg_{index:04d}",
                        "start": index - 1,
                        "end": index,
                        "text": text,
                        "raw_text": text,
                    }
                    for index, text in enumerate(texts, start=1)
                ],
            }
        )

        validation = validate_clinical_workflow(document, policy_index_path=None)

        issue = next(item for item in validation["issues"] if item["rule_id"] == "G04")
        self.assertEqual(issue["field_id"], "past_history")
        self.assertEqual(issue["severity"], "BLOCK")
        self.assertEqual(len(issue["evidence"]), 2)

    def test_g06_blocks_unsupported_medication_details(self):
        document = workflow_document()
        source_text = "혈압약 먹어요"
        document["api3"]["segments"] = [
            {
                "id": "seg_0001",
                "start": 0,
                "end": 2,
                "raw_text": source_text,
                "corrected_text": source_text,
                "annotations": [],
            }
        ]
        document["api2"]["clinical_record"]["medications"] = {
            "items": [
                {
                    "raw_value": "amlodipine 5 mg daily",
                    "status": "confirmed",
                    "evidence": {"source_segment_id": "seg_0001"},
                }
            ]
        }
        field = document["draft"]["fields"]["medications"]
        field.update(
            {
                "value": "amlodipine 5 mg daily",
                "information_status": "PRESENT",
                "evidence": [
                    {
                        "segment_id": "seg_0001",
                        "start": 0,
                        "end": 2,
                        "text": source_text,
                        "raw_text": source_text,
                    }
                ],
            }
        )

        validation = validate_clinical_workflow(document, policy_index_path=None)

        issue = next(item for item in validation["issues"] if item["rule_id"] == "G06")
        self.assertEqual(issue["field_id"], "medications")
        self.assertEqual(issue["severity"], "BLOCK")
        self.assertEqual(field["value"], "amlodipine 5 mg daily")

    def test_g07_blocks_unassessed_allergy_recorded_as_none(self):
        document = workflow_document()
        field = document["draft"]["fields"]["allergy"]
        field.update(
            {
                "value": "없음",
                "information_status": "NONE",
                "evidence": [],
            }
        )

        validation = validate_clinical_workflow(document, policy_index_path=None)

        issue = next(item for item in validation["issues"] if item["rule_id"] == "G07")
        self.assertEqual(issue["field_id"], "allergy")
        self.assertEqual(issue["severity"], "BLOCK")

    def test_g08_blocks_a_plan_not_stated_by_the_clinician(self):
        document = workflow_document()
        source_text = "경과를 보겠습니다"
        document["api3"]["segments"] = [
            {
                "id": "seg_0001",
                "start": 0,
                "end": 2,
                "raw_text": source_text,
                "corrected_text": source_text,
                "annotations": [],
            }
        ]
        document["api2"]["clinical_record"]["treatment_plan"] = [
            {
                "raw_value": "Brain CT 시행",
                "status": "confirmed",
                "evidence": {"source_segment_id": "seg_0001"},
            }
        ]
        field = document["draft"]["fields"]["treatment_plan"]
        field.update(
            {
                "value": "Brain CT 시행",
                "information_status": "PRESENT",
                "evidence": [
                    {
                        "segment_id": "seg_0001",
                        "start": 0,
                        "end": 2,
                        "text": source_text,
                        "raw_text": source_text,
                    }
                ],
            }
        )

        validation = validate_clinical_workflow(document, policy_index_path=None)

        issue = next(item for item in validation["issues"] if item["rule_id"] == "G08")
        self.assertEqual(issue["field_id"], "treatment_plan")
        self.assertEqual(issue["severity"], "BLOCK")
        self.assertEqual(field["value"], "Brain CT 시행")

    def test_g09_blocks_unsupported_physical_exam_without_deleting_the_draft(self):
        document = workflow_document()
        segment = {
            "id": "seg_0001",
            "start": 0,
            "end": 2,
            "raw_text": "배가 아파요",
            "corrected_text": "배가 아파요",
            "annotations": [],
        }
        evidence = {
            "segment_id": "seg_0001",
            "start": 0,
            "end": 2,
            "text": "배가 아파요",
            "raw_text": "배가 아파요",
        }
        document["api3"]["segments"] = [segment]
        document["api2"]["clinical_record"]["physical_examination"] = [
            {
                "raw_value": "rebound tenderness",
                "status": "confirmed",
                "evidence": {"source_segment_id": "seg_0001"},
            }
        ]
        document["draft"]["fields"]["physical_examination"].update(
            {
                "value": "rebound tenderness",
                "information_status": "PRESENT",
                "evidence": [evidence],
            }
        )
        original = copy.deepcopy(document)

        validation = validate_clinical_workflow(document, policy_index_path=None)

        issue = next(item for item in validation["issues"] if item["rule_id"] == "G09")
        self.assertEqual(validation["status"], "BLOCK")
        self.assertEqual(issue["severity"], "BLOCK")
        self.assertEqual(issue["field_id"], "physical_examination")
        self.assertTrue(issue["message"])
        self.assertEqual(issue["evidence"], [evidence])
        self.assertTrue(issue["suggested_action"])
        self.assertEqual(issue["threshold_id"], "V16")
        self.assertEqual(issue["threshold"], "= 0")
        self.assertEqual(issue["policy_evidence"], [])
        self.assertEqual(issue["policy_evidence_status"], "not_applicable")
        self.assertEqual(document, original)

    def test_g19_blocks_ai_generated_completed_state(self):
        document = workflow_document()
        document["record_status"] = "COMPLETED"
        document["completed_at"] = "2026-08-25T12:00:00+09:00"

        validation = validate_clinical_workflow(document, policy_index_path=None)

        issue = next(item for item in validation["issues"] if item["rule_id"] == "G19")
        self.assertEqual(validation["status"], "BLOCK")
        self.assertEqual(issue["field_id"], "record_status")
        self.assertEqual(issue["severity"], "BLOCK")
        self.assertEqual(document["record_status"], "COMPLETED")

    def test_spo2_out_of_range_is_blocked_with_value_range_and_raw_evidence(self):
        document = workflow_document()
        document["api3"]["segments"] = [
            {
                "id": "seg_0001",
                "start": 0,
                "end": 2,
                "raw_text": "산소포화도 289퍼센트입니다",
                "corrected_text": "산소포화도 289퍼센트입니다",
                "annotations": [
                    {
                        "type": "numeric_measurement_candidate",
                        "source_span": {"text": "289퍼센트", "start_char": 6, "end_char": 12},
                        "candidates": [
                            {"kind": "oxygen_saturation", "value": 289, "unit": "%"}
                        ],
                    }
                ],
            }
        ]

        validation = validate_clinical_workflow(document, policy_index_path=None)

        issue = next(
            item for item in validation["issues"] if item["rule_id"] == "VITAL_RANGE"
        )
        self.assertEqual(validation["status"], "BLOCK")
        self.assertEqual(issue["field_id"], "physical_examination")
        self.assertEqual(issue["extracted_value"], 289)
        self.assertEqual(
            issue["allowed_range"], {"minimum": 0, "maximum": 100, "unit": "%"}
        )
        self.assertEqual(issue["raw_value"], "289퍼센트")
        self.assertEqual(issue["evidence"][0]["segment_id"], "seg_0001")
        self.assertEqual(issue["policy_evidence_status"], "not_applicable")

    def test_spo2_88_percent_is_valid(self):
        document = workflow_document()
        document["api3"]["segments"] = [
            {
                "id": "seg_0001",
                "start": 0,
                "end": 2,
                "raw_text": "산소포화도 88퍼센트입니다",
                "corrected_text": "산소포화도 88퍼센트입니다",
                "annotations": [
                    {
                        "type": "numeric_measurement_candidate",
                        "source_span": {"text": "88퍼센트", "start_char": 6, "end_char": 11},
                        "candidates": [
                            {"kind": "oxygen_saturation", "value": 88, "unit": "%"}
                        ],
                    }
                ],
            }
        ]

        validation = validate_clinical_workflow(document, policy_index_path=None)

        self.assertEqual(validation["status"], "PASS")
        self.assertFalse(
            any(issue["rule_id"] == "VITAL_RANGE" for issue in validation["issues"])
        )

    def test_systolic_not_greater_than_diastolic_requires_review(self):
        document = workflow_document()
        document["api3"]["segments"] = [
            {
                "id": "seg_0001",
                "start": 0,
                "end": 2,
                "raw_text": "혈압 80에 90입니다",
                "corrected_text": "혈압 80에 90입니다",
                "annotations": [
                    {
                        "type": "numeric_measurement_candidate",
                        "source_span": {"text": "80에 90", "start_char": 3, "end_char": 9},
                        "candidates": [
                            {
                                "kind": "blood_pressure",
                                "systolic": 80,
                                "diastolic": 90,
                                "unit": "mmHg",
                            }
                        ],
                    }
                ],
            }
        ]

        validation = validate_clinical_workflow(document, policy_index_path=None)

        issue = next(
            item for item in validation["issues"] if item["rule_id"] == "BP_RELATION"
        )
        self.assertEqual(validation["status"], "REVIEW_REQUIRED")
        self.assertEqual(issue["severity"], "REVIEW_REQUIRED")
        self.assertEqual(issue["extracted_value"], {"systolic": 80, "diastolic": 90})

    def test_invalid_information_status_is_blocked_without_rewriting_it(self):
        document = workflow_document()
        field = document["draft"]["fields"]["allergy"]
        field["information_status"] = "UNKNOWN"

        validation = validate_clinical_workflow(document, policy_index_path=None)

        issue = next(
            item
            for item in validation["issues"]
            if item["rule_id"] == "ENUM_VALIDATION"
        )
        self.assertEqual(validation["status"], "BLOCK")
        self.assertEqual(issue["field_id"], "allergy")
        self.assertEqual(issue["severity"], "BLOCK")
        self.assertEqual(issue["value"], "UNKNOWN")
        self.assertEqual(field["information_status"], "UNKNOWN")

    def test_field_misclassification_requires_review_and_preserves_value(self):
        document = workflow_document()
        text = "어지러움"
        document["api3"]["segments"] = [
            {
                "id": "seg_0001",
                "start": 0,
                "end": 1,
                "raw_text": text,
                "corrected_text": text,
                "annotations": [],
            }
        ]
        document["api2"]["clinical_record"]["pain_assessment"] = {
            "nrs": {
                "raw_value": text,
                "status": "confirmed",
                "evidence": {"source_segment_id": "seg_0001"},
            }
        }
        field = document["draft"]["fields"]["pain_assessment"]
        field.update(
            {
                "value": text,
                "information_status": "PRESENT",
                "evidence": [
                    {
                        "segment_id": "seg_0001",
                        "start": 0,
                        "end": 1,
                        "text": text,
                        "raw_text": text,
                    }
                ],
            }
        )

        validation = validate_clinical_workflow(document, policy_index_path=None)

        issue = next(
            item
            for item in validation["issues"]
            if item["rule_id"] == "FIELD_MISCLASSIFICATION"
        )
        self.assertEqual(validation["status"], "REVIEW_REQUIRED")
        self.assertEqual(issue["field_id"], "pain_assessment")
        self.assertEqual(issue["severity"], "REVIEW_REQUIRED")
        self.assertEqual(issue["policy_evidence_status"], "not_applicable")
        self.assertEqual(field["value"], text)

    def test_partial_processing_requires_review(self):
        document = workflow_document()
        document["processing_status"] = "partial"
        document["errors"] = [
            {"stage": "api2", "code": "RuntimeError", "detail": "model failed"}
        ]

        validation = validate_clinical_workflow(document, policy_index_path=None)

        issue = next(
            item
            for item in validation["issues"]
            if item["rule_id"] == "PROCESSING_STATUS"
        )
        self.assertEqual(validation["status"], "REVIEW_REQUIRED")
        self.assertEqual(issue["severity"], "REVIEW_REQUIRED")

    def test_policy_retrieval_failure_never_changes_the_guardrail_decision(self):
        document = workflow_document()
        document["record_status"] = "COMPLETED"
        document["workflow_phase"] = "FINALIZATION"

        def available(rule_id, query):
            self.assertEqual(rule_id, "G19")
            self.assertTrue(query)
            return {
                "rule_id": rule_id,
                "results": [
                    {
                        "source_id": "S04",
                        "chunk_id": "S04-p007-c01",
                        "title": "WHO Ethics and governance of AI for health",
                        "page": 7,
                        "section": "Accountability",
                        "excerpt": "Responsibility for decision-making with AI.",
                        "retrieval_score": 0.8,
                    }
                ],
            }

        def unavailable(rule_id, query):
            del rule_id, query
            raise OSError("policy database unavailable")

        with_policy = validate_clinical_workflow(
            document,
            policy_evidence_provider=available,
        )
        without_policy = validate_clinical_workflow(
            document,
            policy_evidence_provider=unavailable,
        )

        self.assertEqual(with_policy["status"], "BLOCK")
        self.assertEqual(without_policy["status"], "BLOCK")
        with_issue = next(
            item for item in with_policy["issues"] if item["rule_id"] == "G19"
        )
        without_issue = next(
            item for item in without_policy["issues"] if item["rule_id"] == "G19"
        )
        self.assertEqual(with_issue["policy_evidence_status"], "available")
        self.assertTrue(with_issue["policy_evidence"])
        self.assertEqual(without_issue["policy_evidence_status"], "unavailable")
        self.assertEqual(without_issue["policy_evidence"], [])
        for key in (
            "rule_id",
            "severity",
            "field_id",
            "message",
            "evidence",
            "suggested_action",
        ):
            self.assertEqual(with_issue[key], without_issue[key])

    def test_draft_phase_keeps_the_block_without_attaching_policy_documents(self):
        document = workflow_document()
        document["workflow_phase"] = "DRAFT_GENERATION"
        document["api2"]["clinical_record"]["chief_complaint"] = {
            "raw_value": "pneumonia",
            "status": "confirmed",
            "evidence": None,
        }
        document["draft"]["fields"]["chief_complaint"].update(
            {
                "value": "pneumonia",
                "information_status": "PRESENT",
                "evidence": [],
            }
        )
        provider_calls = []

        def provider(rule_id, query):
            provider_calls.append((rule_id, query))
            return {
                "rule_id": rule_id,
                "results": [{"source_id": "S01", "title": "의료법 제22조"}],
            }

        validation = validate_clinical_workflow(
            document,
            policy_evidence_provider=provider,
        )

        issue = next(item for item in validation["issues"] if item["rule_id"] == "G01")
        self.assertEqual(validation["status"], "BLOCK")
        self.assertEqual(issue["policy_evidence"], [])
        self.assertEqual(issue["policy_evidence_status"], "not_applicable")
        self.assertEqual(provider_calls, [])

    def test_same_input_always_returns_the_same_validation_result(self):
        document = workflow_document()
        document["record_status"] = "COMPLETED"

        first = validate_clinical_workflow(document, policy_index_path=None)
        second = validate_clinical_workflow(document, policy_index_path=None)

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

