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
                "drug_allergy",
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
                "6ab1dc3e4370fc5c566ee1c05d97df2fc1e489d46d04674977835be4f3031cae"
            ),
            "candidate_adjudication_v1.txt": (
                "a9955ec10b509cdb86ab9fa0ec3dc4f7a4604001fd9dfdea737ab65ad70caca6"
            ),
            "draft_normalization_v1.txt": (
                "77a88ee1df3baa39d939d630d26e88a6f1d943893b9dd88b325db4732d219a19"
            ),
            "compact_record_output_v3.txt": (
                "ebc859a02df4b1b9027101b637cc73c1ebb87fd34630d8dbb286cbd0983f47e2"
            ),
            "compact_record_output_v3_1_lean.txt": (
                "1c2ec2cf75fe8fdf47380a76b8e44b1d3fef45118db1bf45de64147962bee02b"
            ),
            "compact_fact_output_v1.txt": (
                "bc2b4f35441cd885a3e2d1225652a7d7796222fa4072408aca6c91c7c8d15704"
            ),
            "compact_fields_output_v1.txt": (
                "a79336dd368f53727d9545c3e50226a0c6928d2b8040c07493b72067407531ea"
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
                "befa840306589b60ce6cb12554f7eb8cb6b72937d5d3938388e1a535cbdbb7ec"
            ),
            "prompts/clinical_record_extraction_v2.txt": (
                "6ab1dc3e4370fc5c566ee1c05d97df2fc1e489d46d04674977835be4f3031cae"
            ),
            "prompts/candidate_adjudication_v1.txt": (
                "a9955ec10b509cdb86ab9fa0ec3dc4f7a4604001fd9dfdea737ab65ad70caca6"
            ),
            "prompts/draft_normalization_v1.txt": (
                "77a88ee1df3baa39d939d630d26e88a6f1d943893b9dd88b325db4732d219a19"
            ),
            "prompts/compact_record_output_v3.txt": (
                "ebc859a02df4b1b9027101b637cc73c1ebb87fd34630d8dbb286cbd0983f47e2"
            ),
            "prompts/compact_record_output_v3_1_lean.txt": (
                "1c2ec2cf75fe8fdf47380a76b8e44b1d3fef45118db1bf45de64147962bee02b"
            ),
            "prompts/compact_fact_output_v1.txt": (
                "bc2b4f35441cd885a3e2d1225652a7d7796222fa4072408aca6c91c7c8d15704"
            ),
            "prompts/compact_fields_output_v1.txt": (
                "a79336dd368f53727d9545c3e50226a0c6928d2b8040c07493b72067407531ea"
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
