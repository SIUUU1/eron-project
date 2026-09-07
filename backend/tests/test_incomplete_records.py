"""대시보드 기록 미완료 판정 회귀 테스트.

    python -m unittest tests.test_incomplete_records -v
"""

import unittest

from app.services.ed import to_incomplete_record_items


REQUIRED_RECORD = {
    "chiefComplaint": "Cough",
    "painAssessment": "NRS 3",
    "presentIllness": "3일 전부터 기침 있음.",
    "pastHistory": "HTN(+)",
    "medication": "Amlodipine",
    "allergy": "NONE",
    "socialHistory": "Smoking: Never",
    "systemReview": "Cough(+)",
    "physicalExam": "Chest: Clear",
    "outcome": "입원",
}


def row(stay_id: int, status: str | None, payload=None) -> dict:
    return {
        "stay_id": stay_id,
        "display_name": f"환자 {stay_id}",
        "record_status": status,
        "record_payload": payload,
    }


class IncompleteRecordItemsTests(unittest.TestCase):
    def test_includes_unwritten_and_missing_draft_only(self):
        missing_payload = {
            "record": {**REQUIRED_RECORD, "allergy": ""},
            "field_statuses": {"allergy": "missing"},
        }
        complete_payload = {"record": REQUIRED_RECORD, "field_statuses": None}

        items, count = to_incomplete_record_items(
            [
                row(1, None),
                row(2, "DRAFT", missing_payload),
                row(3, "DRAFT", complete_payload),
                row(4, "SIGNED", missing_payload),
            ],
            limit=5,
        )

        self.assertEqual(count, 2)
        self.assertEqual([item.stay_id for item in items], ["1", "2"])
        self.assertEqual(items[0].reason, "RECORD_NOT_CREATED")
        self.assertEqual(items[0].missing_fields, [])
        self.assertEqual(items[1].reason, "MISSING_REQUIRED_FIELDS")
        self.assertEqual(items[1].missing_fields, ["allergy"])

    def test_manual_draft_without_statuses_falls_back_to_record_values(self):
        payload = {
            "record": {
                **REQUIRED_RECORD,
                "painAssessment": "미확인",
                "outcome": "선택되지 않음",
            }
        }

        items, count = to_incomplete_record_items([row(10, "DRAFT", payload)], limit=5)

        self.assertEqual(count, 1)
        self.assertEqual(items[0].missing_fields, ["pain_assessment", "outcome"])

    def test_review_status_is_not_counted_as_missing(self):
        statuses = {key: "complete" for key in REQUIRED_RECORD}
        statuses["pastHistory"] = "review"
        payload = {"record": REQUIRED_RECORD, "field_statuses": statuses}

        items, count = to_incomplete_record_items([row(20, "DRAFT", payload)], limit=5)

        self.assertEqual(count, 0)
        self.assertEqual(items, [])

    def test_limit_does_not_change_total_count(self):
        rows = [row(stay_id, None) for stay_id in range(1, 8)]

        items, count = to_incomplete_record_items(rows, limit=3)

        self.assertEqual(count, 7)
        self.assertEqual([item.stay_id for item in items], ["1", "2", "3"])


if __name__ == "__main__":
    unittest.main()
