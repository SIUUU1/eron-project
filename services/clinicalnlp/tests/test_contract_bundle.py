import json
import hashlib
from pathlib import Path
import unittest


SERVICE_ROOT = Path(__file__).parents[1]


class ClinicalContractBundleTests(unittest.TestCase):
    def test_impression_prompt_preserves_certainty_and_blocks_model_diagnosis(self):
        prompt = (
            SERVICE_ROOT / "prompts" / "clinical_record_extraction_v2.txt"
        ).read_text(encoding="utf-8")

        self.assertIn("clinician's current assessment", prompt)
        self.assertIn("`R/O ` display prefix", prompt)
        self.assertIn("Preserve every explicitly stated differential", prompt)
        self.assertIn("self-diagnosis", prompt)
        self.assertIn("Never invent a KCD code", prompt)

    def test_outcome_prompt_separates_final_disposition_from_plan(self):
        prompt = (
            SERVICE_ROOT / "prompts" / "clinical_record_extraction_v2.txt"
        ).read_text(encoding="utf-8")

        self.assertIn("final, current-encounter disposition decision", prompt)
        self.assertIn("Discharge, Admission, Transfer, Death, or Other", prompt)
        self.assertIn("Never turn a considered, planned, possible, or conditional", prompt)
        self.assertIn("We will proceed with admission.", prompt)
        self.assertIn("입원 진행하겠습니다.", prompt)
        self.assertIn("Admission is being considered.", prompt)
        self.assertIn("We will decide after the CT result.", prompt)
        self.assertIn("The patient may require admission.", prompt)
        self.assertIn("Death requires an explicit clinician confirmation", prompt)
        self.assertIn("Never use NONE for outcome", prompt)

    def test_high_risk_fields_use_bounded_fact_types_without_diarization(self):
        prompt = (
            SERVICE_ROOT / "prompts" / "clinical_record_extraction_v2.txt"
        ).read_text(encoding="utf-8")

        self.assertIn('"fact_type":"EXAM|UNKNOWN"', prompt)
        self.assertIn('"fact_type":"ASSESSMENT|UNKNOWN"', prompt)
        self.assertIn('"fact_type":"PLAN|UNKNOWN"', prompt)
        self.assertIn('"fact_type":"OUTCOME|UNKNOWN"', prompt)
        self.assertNotIn("speaker_role", prompt)

    def test_compact_prompt_binds_candidate_reference_to_snapshot_segment(self):
        prompt = (
            SERVICE_ROOT / "prompts" / "compact_record_output_v3.txt"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "segments array must include that snapshot's exact segment_id",
            prompt,
        )
        self.assertIn("Never reuse a candidate snapshot", prompt)

    def test_workflow_schema_preserves_the_public_draft_interface(self):
        schema_path = (
            SERVICE_ROOT / "contracts" / "clinical-workflow-v2.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(schema["$id"], "urn:eron:clinical-workflow-v2")
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            "clinical-workflow-v2",
        )
        self.assertEqual(
            schema["properties"]["processing_status"]["enum"],
            ["completed", "partial", "failed"],
        )
        self.assertEqual(
            schema["properties"]["record_status"]["enum"],
            ["NOT_STARTED", "DRAFT", "COMPLETED"],
        )
        self.assertEqual(
            schema["properties"]["draft"]["properties"]["fields"]["required"],
            [
                "chief_complaint",
                "pain_assessment",
                "history_of_present_illness",
                "past_history",
                "medications",
                "allergy",
                "social_history",
                "review_of_systems",
                "physical_examination",
                "treatment_plan",
                "impression",
                "outcome",
            ],
        )
        version_properties = schema["properties"]["audit"]["properties"][
            "versions"
        ]["properties"]
        self.assertIn("draft_normalization_prompt", version_properties)
        self.assertIn("compact_prompt", version_properties)
        reference_properties = schema["properties"]["audit"]["properties"][
            "references"
        ]["properties"]
        self.assertIn("compact_record_path", reference_properties)

    def test_backend_and_clinicalnlp_workflow_schemas_match(self):
        service_schema = json.loads(
            (SERVICE_ROOT / "contracts" / "clinical-workflow-v2.schema.json").read_text(
                encoding="utf-8"
            )
        )
        backend_schema = json.loads(
            (
                SERVICE_ROOT.parents[1]
                / "backend"
                / "app"
                / "contracts"
                / "clinical-workflow-v2.schema.json"
            ).read_text(encoding="utf-8")
        )
        package_schema = json.loads(
            (
                SERVICE_ROOT
                / "clinicalnlp_api3"
                / "contracts"
                / "clinical-workflow-v2.schema.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(backend_schema, service_schema)
        self.assertEqual(package_schema, service_schema)

    def test_prompt_bundle_matches_the_approved_source_versions(self):
        expected_hashes = {
            "clinical_record_extraction_v2.txt": (
                "4662a50c5aaf2bc8bd712d257f77f28f9823043b80bd3b64cd822d0c0e1ee52e"
            ),
            "candidate_adjudication_v1.txt": (
                "262a30e0f846d69376828add354bd98eccedbb83ecb4e0a107b99d96815d2a9e"
            ),
            "draft_normalization_v1.txt": (
                "0d8d686d26b7efde5f2d502037528737fe1fdbd16a80bb6da69ccad6570bfe66"
            ),
            "compact_record_output_v3.txt": (
                "ebc859a02df4b1b9027101b637cc73c1ebb87fd34630d8dbb286cbd0983f47e2"
            ),
            "compact_record_output_v3_1_lean.txt": (
                "c3e30437d3d6fcaf54643f580b8cd5b912e150488471e352d89bfdb84eecea26"
            ),
            "compact_fact_output_v1.txt": (
                "c37a2f61a11a1e348d454918dd07c813c13fe303eb666889201676b01768624f"
            ),
            "compact_fields_output_v1.txt": (
                "b239b8f8bf50c74c790152928c0bdb4d5d813294745c2ab30f15c1842b75a4d5"
            ),
        }

        actual_hashes = {
            name: hashlib.sha256(
                (SERVICE_ROOT / "prompts" / name).read_bytes()
            ).hexdigest()
            for name in expected_hashes
        }

        self.assertEqual(actual_hashes, expected_hashes)

    def test_manifest_pins_every_contract_asset_by_hash(self):
        manifest = json.loads(
            (SERVICE_ROOT / "contract-manifest.json").read_text(encoding="utf-8")
        )
        expected_assets = {
            "contracts/clinical-workflow-v2.schema.json": (
                "d28ed3ef3f98e3afaa364c49d9ecfaabcbfb0443f74baf3f64c94f4d515b3116"
            ),
            "prompts/clinical_record_extraction_v2.txt": (
                "4662a50c5aaf2bc8bd712d257f77f28f9823043b80bd3b64cd822d0c0e1ee52e"
            ),
            "prompts/candidate_adjudication_v1.txt": (
                "262a30e0f846d69376828add354bd98eccedbb83ecb4e0a107b99d96815d2a9e"
            ),
            "prompts/draft_normalization_v1.txt": (
                "0d8d686d26b7efde5f2d502037528737fe1fdbd16a80bb6da69ccad6570bfe66"
            ),
            "prompts/compact_record_output_v3.txt": (
                "ebc859a02df4b1b9027101b637cc73c1ebb87fd34630d8dbb286cbd0983f47e2"
            ),
            "prompts/compact_record_output_v3_1_lean.txt": (
                "c3e30437d3d6fcaf54643f580b8cd5b912e150488471e352d89bfdb84eecea26"
            ),
            "prompts/compact_fact_output_v1.txt": (
                "c37a2f61a11a1e348d454918dd07c813c13fe303eb666889201676b01768624f"
            ),
            "prompts/compact_fields_output_v1.txt": (
                "b239b8f8bf50c74c790152928c0bdb4d5d813294745c2ab30f15c1842b75a4d5"
            ),
        }

        self.assertEqual(
            manifest["schema_version"], "clinicalnlp-contract-bundle-v1"
        )
        self.assertEqual(
            {asset["path"]: asset["sha256"] for asset in manifest["assets"]},
            expected_assets,
        )
        self.assertEqual(
            {
                asset["path"]: hashlib.sha256(
                    (SERVICE_ROOT / asset["path"]).read_bytes()
                ).hexdigest()
                for asset in manifest["assets"]
            },
            expected_assets,
        )


if __name__ == "__main__":
    unittest.main()
