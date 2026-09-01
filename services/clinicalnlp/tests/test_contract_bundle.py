import json
import hashlib
from pathlib import Path
import unittest


SERVICE_ROOT = Path(__file__).parents[1]


class ClinicalContractBundleTests(unittest.TestCase):
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

    def test_prompt_bundle_matches_the_approved_source_versions(self):
        expected_hashes = {
            "clinical_record_extraction_v2.txt": (
                "34efdb7d7fd5b8dbb9c51d4d8fb2d9892ae367e75784877d15fc00ee425baf2a"
            ),
            "candidate_adjudication_v1.txt": (
                "262a30e0f846d69376828add354bd98eccedbb83ecb4e0a107b99d96815d2a9e"
            ),
            "draft_normalization_v1.txt": (
                "75d0735d66183c46b4d640e718b07f8463f8a899f5f6c05d1658fa7c3664fa1f"
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
                "1707d4bc4c6e6dcd695832ce349a80c8fe997392efe871f39bbb51f80f0dc3e4"
            ),
            "prompts/clinical_record_extraction_v2.txt": (
                "34efdb7d7fd5b8dbb9c51d4d8fb2d9892ae367e75784877d15fc00ee425baf2a"
            ),
            "prompts/candidate_adjudication_v1.txt": (
                "262a30e0f846d69376828add354bd98eccedbb83ecb4e0a107b99d96815d2a9e"
            ),
            "prompts/draft_normalization_v1.txt": (
                "75d0735d66183c46b4d640e718b07f8463f8a899f5f6c05d1658fa7c3664fa1f"
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
