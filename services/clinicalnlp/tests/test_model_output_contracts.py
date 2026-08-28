import unittest

from clinicalnlp_api3.model_output_contracts import (
    candidate_adjudication_response_format,
    clinical_record_response_format,
    draft_normalization_response_format,
    translation_search_response_format,
)


def _schema(response_format):
    return response_format["json_schema"]["schema"]


class ModelOutputContractTests(unittest.TestCase):
    def test_each_gemma_task_has_an_independent_output_contract(self):
        translation = translation_search_response_format(["seg_0001"])
        clinical_record = clinical_record_response_format(["seg_0001"])
        candidate_adjudication = candidate_adjudication_response_format()
        draft_normalization = draft_normalization_response_format(
            ["impression"],
            ["impression:0:seg_0001"],
            ["candidate:1"],
        )

        self.assertEqual(
            translation["json_schema"]["name"],
            "full_segment_medical_translation",
        )
        self.assertEqual(
            clinical_record["json_schema"]["name"],
            "clinical_record_extraction",
        )
        self.assertEqual(
            candidate_adjudication["json_schema"]["name"],
            "candidate_adjudication",
        )
        self.assertEqual(
            draft_normalization["json_schema"]["name"],
            "clinical_draft_normalization",
        )
        self.assertEqual(set(_schema(translation)["required"]), {"segments"})
        self.assertEqual(
            set(_schema(clinical_record)["required"]),
            {"clinical_record", "unresolved_questions"},
        )
        self.assertEqual(
            set(_schema(candidate_adjudication)["required"]),
            {"candidate_decisions"},
        )
        self.assertEqual(
            set(_schema(draft_normalization)["required"]),
            {"draft_suggestions"},
        )
        self.assertNotIn(
            "candidate_decisions", _schema(clinical_record)["properties"]
        )
        self.assertNotIn(
            "clinical_record", _schema(candidate_adjudication)["properties"]
        )

    def test_candidate_contract_is_fixed_and_grounded_after_generation(self):
        contract = candidate_adjudication_response_format()
        decision = _schema(contract)["properties"]["candidate_decisions"]["items"]

        self.assertEqual(decision["properties"]["segment_id"], {"type": "string"})
        self.assertEqual(
            decision["properties"]["annotation_index"],
            {"type": "integer", "minimum": 0},
        )
        self.assertEqual(
            decision["properties"]["selected_candidate_ids"]["items"],
            {"type": "string"},
        )
        self.assertEqual(
            decision["properties"]["selected_candidate_ids"]["maxItems"], 2
        )
        self.assertNotIn("oneOf", decision)
        self.assertNotIn("enum", decision["properties"]["segment_id"])
        self.assertNotIn(
            "enum",
            decision["properties"]["selected_candidate_ids"]["items"],
        )

    def test_segment_bound_contracts_reject_unknown_segment_ids(self):
        translation = _schema(translation_search_response_format(["seg_0001"]))
        clinical_record = _schema(clinical_record_response_format(["seg_0001"]))

        translated_segment_id = translation["properties"]["segments"]["items"][
            "properties"
        ]["segment_id"]
        unresolved_segment_id = clinical_record["properties"][
            "unresolved_questions"
        ]["items"]["properties"]["source_segment_id"]

        self.assertEqual(translated_segment_id["enum"], ["seg_0001"])
        self.assertEqual(unresolved_segment_id["enum"], ["seg_0001"])


if __name__ == "__main__":
    unittest.main()

