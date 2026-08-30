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
                "drug_allergy",
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
                "a61583aa62ba4ccf5af35d07eee32cd84816980859ad183cdb9e816d95bc7120"
            ),
            "candidate_adjudication_v1.txt": (
                "a9955ec10b509cdb86ab9fa0ec3dc4f7a4604001fd9dfdea737ab65ad70caca6"
            ),
            "draft_normalization_v1.txt": (
                "69bd2f91a65bf8d9d61126932535145d5511e6d81bda3306969056e5583a1f3b"
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
                "fe59f3d8e8d2feea97313d6288bc88731be3d160aa4154cf4d38692a29cd5550"
            ),
            "prompts/clinical_record_extraction_v2.txt": (
                "a61583aa62ba4ccf5af35d07eee32cd84816980859ad183cdb9e816d95bc7120"
            ),
            "prompts/candidate_adjudication_v1.txt": (
                "a9955ec10b509cdb86ab9fa0ec3dc4f7a4604001fd9dfdea737ab65ad70caca6"
            ),
            "prompts/draft_normalization_v1.txt": (
                "69bd2f91a65bf8d9d61126932535145d5511e6d81bda3306969056e5583a1f3b"
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
