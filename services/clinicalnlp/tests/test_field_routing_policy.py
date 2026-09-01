import unittest

from clinicalnlp_api3.field_routing_policy import (
    FieldEvidence,
    candidate_allowed_for_field,
    choose_evidence_field,
    evidence_fields_by_segment,
    field_collection_hints_by_segment,
    filter_candidates_for_field,
)
from clinicalnlp_api3.workflow import build_draft


def _candidate(
    entity_id,
    canonical_en,
    semantic_types,
    *,
    collection="emergency_terms",
    entity_type=None,
):
    return {
        "collection": collection,
        "entity_id": entity_id,
        "canonical_ko": "",
        "canonical_en": canonical_en,
        "entity_type": entity_type,
        "match_type": "umls_dictionary_search",
        "retrieval_score": 0.95,
        "provenance": {
            "source": "UMLS",
            "cui": f"C{entity_id}",
            "semantic_types": semantic_types,
            "similarity": 0.94,
        },
    }


def _atom(raw_value, segment_id="seg_0001", status="confirmed"):
    return {
        "raw_value": raw_value,
        "status": status,
        "evidence": {"source_segment_id": segment_id},
    }


class FieldRoutingPolicyTests(unittest.TestCase):
    def test_impression_policy_requires_an_explicit_clinician_assessment(self):
        from clinicalnlp_api3.field_routing_policy import FIELD_POLICIES

        description = FIELD_POLICIES["impression"].definition

        self.assertIn("Clinician-explicit", description)
        self.assertIn("patient self-diagnoses", description)
        self.assertIn("never create an impression", description)

    def test_outcome_policy_requires_a_final_clinician_disposition(self):
        from clinicalnlp_api3.field_routing_policy import FIELD_POLICIES

        definition = FIELD_POLICIES["outcome"].definition

        self.assertIn("explicit final clinician disposition", definition)
        self.assertIn("conditional plans", definition)
        self.assertEqual(FIELD_POLICIES["outcome"].allowed_term_types, frozenset())

    def test_field_collection_hints_are_derived_from_grounded_record_fields(self):
        record = {
            "medications": [_atom("암로디핀 복용 중")],
            "treatment_plan": [_atom("흉부 CT를 시행합니다.")],
            "social_history": {"smoking": _atom("흡연력 미확인")},
        }

        routed = field_collection_hints_by_segment(record)

        self.assertEqual(
            routed,
            {
                "seg_0001": frozenset(
                    {"drug_terms", "procedure_terms"}
                )
            },
        )

    def test_timestamp_evidence_is_mapped_back_to_its_source_segment(self):
        record = {
            "impression": [{
                "raw_value": "폐렴 의심",
                "status": "needs_confirmation",
                "evidence": {"start": 4.0, "end": 6.0, "text": "..."},
            }]
        }

        routed = field_collection_hints_by_segment(
            record,
            segments=[{
                "id": "seg_0003",
                "start": 4.0,
                "end": 6.0,
                "text": "...",
            }],
        )

        self.assertEqual(
            routed,
            {"seg_0003": frozenset({"emergency_terms"})},
        )

    def test_evidence_index_preserves_multiple_atomic_fields_for_one_segment(self):
        record = {
            "history_of_present_illness": {
                "course": _atom("어제부터 숨이 찼습니다.")
            },
            "physical_examination": [_atom("산소포화도 88%")],
            "impression": [_atom("폐렴이 의심됩니다.", status="needs_confirmation")],
        }

        routed = evidence_fields_by_segment(record)

        self.assertEqual(
            [(item.field_id, item.raw_value) for item in routed["seg_0001"]],
            [
                ("history_of_present_illness", "어제부터 숨이 찼습니다."),
                ("physical_examination", "산소포화도 88%"),
                ("impression", "폐렴이 의심됩니다."),
            ],
        )

    def test_exact_atomic_span_routes_disease_to_past_history_not_impression(self):
        disease = _candidate("copd", "COPD", ["T047"])
        evidence = (
            FieldEvidence("past_history", "COPD 병력이 있습니다."),
            FieldEvidence("impression", "폐렴이 의심됩니다."),
        )

        field_id = choose_evidence_field(
            evidence,
            source_text="COPD",
            candidates=[disease],
            annotation_term_type="disease_or_diagnosis",
        )

        self.assertEqual(field_id, "past_history")

    def test_exact_grounded_sentence_is_not_rerouted_by_an_unrelated_drug_hit(self):
        unrelated_drug = _candidate(
            "yellow-compound",
            "Astragalus Root/Lonicera Flower/Yellow Beeswax",
            [],
            collection="drug_terms",
            entity_type="product",
        )
        source = "The sputum has turned more yellow than usual."
        evidence = (
            FieldEvidence("history_of_present_illness", source),
            FieldEvidence("medications", "Amlodipine"),
        )

        field_id = choose_evidence_field(
            evidence,
            source_text=source,
            candidates=[unrelated_drug],
        )

        self.assertIsNone(field_id)

    def test_review_items_keep_only_candidate_that_matches_grounded_draft_field(self):
        source = "The sputum has turned more yellow than usual."
        unrelated_drug = _candidate(
            "yellow-compound",
            "Astragalus Root/Lonicera Flower/Yellow Beeswax",
            [],
            collection="drug_terms",
            entity_type="product",
        )
        sputum = _candidate(
            "sputum",
            "Sputum",
            ["T184"],
        )
        api3 = {
            "segments": [{
                "id": "seg_0001",
                "start": 0.0,
                "end": 3.0,
                "raw_text": source,
                "annotations": [{
                    "type": "medical_term_candidate",
                    "source_span": {
                        "text": source,
                        "start_char": 0,
                        "end_char": len(source),
                    },
                    "candidates": [unrelated_drug],
                    "needs_review": True,
                }, {
                    "type": "medical_term_candidate",
                    "source_span": {
                        "text": source,
                        "start_char": 0,
                        "end_char": len(source),
                    },
                    "candidates": [sputum],
                    "needs_review": True,
                }],
            }]
        }
        api2 = {
            "clinical_record": {
                "history_of_present_illness": {
                    "course": _atom(source),
                },
                "medications": [_atom("Amlodipine")],
            }
        }

        draft = build_draft(api2, api3)

        self.assertEqual(len(draft["review_items"]), 1)
        self.assertEqual(draft["review_items"][0]["field_id"], "history")
        self.assertEqual(draft["review_items"][0]["candidates"], ["Sputum"])

    def test_umls_semantic_type_overrides_misleading_collection(self):
        disease_in_drug_collection = _candidate(
            "pneumonia",
            "Pneumonia",
            ["T047"],
            collection="drug_terms",
            entity_type="product",
        )

        self.assertTrue(
            candidate_allowed_for_field("impression", disease_in_drug_collection)
        )
        self.assertFalse(
            candidate_allowed_for_field("medication", disease_in_drug_collection)
        )

    def test_field_filter_keeps_only_compatible_candidate_types(self):
        disease = _candidate("pneumonia", "Pneumonia", ["T047"])
        drug = _candidate(
            "amlodipine",
            "Amlodipine",
            ["T121"],
            collection="drug_terms",
            entity_type="ingredient",
        )

        filtered = filter_candidates_for_field(
            "impression",
            [drug, disease],
            annotation_term_type="disease_or_diagnosis",
        )

        self.assertEqual([item["entity_id"] for item in filtered], ["pneumonia"])

    def test_review_items_use_clinical_evidence_and_filter_candidate_provenance(self):
        disease = _candidate("pneumonia", "Pneumonia", ["T047"])
        drug = _candidate(
            "amlodipine",
            "Amlodipine",
            ["T121"],
            collection="drug_terms",
            entity_type="ingredient",
        )
        api3 = {
            "segments": [
                {
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 2.0,
                    "raw_text": "폐렴이 의심되어 흉부 CT를 시행하겠습니다.",
                    "annotations": [
                        {
                            "type": "medical_term_candidate",
                            "term_type": "disease_or_diagnosis",
                            "source_span": {
                                "text": "폐렴",
                                "start_char": 0,
                                "end_char": 2,
                            },
                            "candidates": [drug, disease],
                            "needs_review": True,
                        },
                        {
                            "type": "medical_term_candidate",
                            "term_type": "test_procedure_or_surgery",
                            "source_span": {
                                "text": "흉부 CT",
                                "start_char": 9,
                                "end_char": 14,
                            },
                            "candidates": [
                                _candidate(
                                    "chest-ct",
                                    "Chest CT",
                                    ["T060"],
                                    collection="procedure_terms",
                                )
                            ],
                            "needs_review": True,
                        },
                    ],
                }
            ]
        }
        api2 = {
            "clinical_record": {
                "impression": [
                    _atom("폐렴이 의심되어", status="needs_confirmation")
                ],
                "treatment_plan": [_atom("흉부 CT를 시행하겠습니다.")],
            }
        }

        draft = build_draft(api2, api3)

        self.assertEqual(
            [item["field_id"] for item in draft["review_items"]],
            ["impression", "treatment-plan"],
        )
        impression = draft["review_items"][0]
        self.assertEqual(impression["candidates"], ["Pneumonia"])
        self.assertEqual(
            [item["display_value"] for item in impression["candidate_provenance"]],
            ["Pneumonia"],
        )


if __name__ == "__main__":
    unittest.main()
