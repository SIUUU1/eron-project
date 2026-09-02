import unittest

from scripts.compare_compact_v3 import _summary


class CompareCompactV3SummaryTests(unittest.TestCase):
    def test_primary_rollout_modes_read_compact_primary_result(self):
        result = {
            "processing_status": "completed",
            "compact_v3_primary": {
                "status": "available",
                "record": {
                    "fields": {
                        "chief_complaint": {
                            "text": "Cough",
                            "fact_refs": ["f1"],
                        }
                    }
                },
                "validation": {
                    "status": "PASS",
                    "issues": [],
                    "summary": {"issue_count": 0},
                },
            },
        }

        for mode in ("primary", "legacy", "lean_primary"):
            with self.subTest(mode=mode):
                summary = _summary(
                    result,
                    include_field_text=True,
                    mode=mode,
                )
                compact = summary["compact_v3"]
                self.assertEqual(compact["status"], "available")
                self.assertEqual(
                    compact["fields"]["chief_complaint"]["text"],
                    "Cough",
                )


if __name__ == "__main__":
    unittest.main()
