"""15분 슬롯 예측 스케줄러 — 슬롯 배정·due 판정·batch 구성 회귀 테스트.

    python -m unittest tests.test_prediction_schedule -v
"""

import unittest
from datetime import datetime, timedelta

from app.services.prediction_runner import (
    current_slot,
    is_due,
    select_stays,
    slot_for,
)


def t(hhmm: str) -> datetime:
    """'11:07' → 2026-01-01 11:07."""
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(2026, 1, 1, hour, minute)


def stay(stay_id: int, next_at: datetime, t_now: datetime, *,
         offset: timedelta = timedelta(0), outtime: datetime | None = None) -> dict:
    return {
        "stay_id": stay_id,
        "next_prediction_at": next_at,
        "t_now": t_now,
        "demo_offset": offset,
        "outtime": outtime,
    }


class SlotCeiling(unittest.TestCase):
    """Test 1 — 15분 올림(ceiling) 규칙."""

    def test_ceiling_rule(self):
        cases = {
            "11:00": "11:00", "11:01": "11:15", "11:07": "11:15",
            "11:15": "11:15", "11:16": "11:30", "11:18": "11:30",
            "11:30": "11:30", "11:31": "11:45", "11:45": "11:45",
            "11:46": "12:00", "11:47": "12:00",
        }
        for given, expected in cases.items():
            with self.subTest(given=given):
                self.assertEqual(slot_for(t(given)), t(expected))

    def test_seconds_move_to_next_slot(self):
        # 11:00:01 은 11:00 슬롯에서 계산할 수 없다(그 시점 데이터가 아직 없다).
        self.assertEqual(slot_for(t("11:00") + timedelta(seconds=1)), t("11:15"))

    def test_current_slot_is_floor(self):
        self.assertEqual(current_slot(t("11:10")), t("11:00"))
        self.assertEqual(current_slot(t("11:15")), t("11:15"))
        self.assertEqual(current_slot(t("11:59")), t("11:45"))


class DueFiltering(unittest.TestCase):
    """Test 4 — 미래 예측을 미리 계산하지 않는다."""

    def test_due_predicate(self):
        now = t("11:10")
        self.assertTrue(is_due(stay(1, t("11:07"), now)))   # 지났다
        self.assertFalse(is_due(stay(2, t("11:12"), now)))  # 아직이다

    def test_future_stay_never_selected(self):
        now = t("11:10")
        picked = select_stays([stay(2, t("11:12"), now)], slot_limit=current_slot(now))
        self.assertEqual(picked, [])


class BatchGrouping(unittest.TestCase):
    """Test 3 — 같은 슬롯 환자가 한 batch 로 묶인다."""

    def setUp(self):
        self.now = t("11:40")
        self.stays = [
            stay(ord("A"), t("11:07"), self.now),
            stay(ord("B"), t("11:12"), self.now),
            stay(ord("C"), t("11:18"), self.now),
            stay(ord("D"), t("11:29"), self.now),
        ]

    def _ids(self, slot_limit):
        return [s["stay_id"] for s in select_stays(self.stays, slot_limit=slot_limit)]

    def test_slot_1115_batch(self):
        # 11:15 슬롯에는 A·B 만 (C·D 는 11:30 슬롯)
        self.assertEqual(self._ids(t("11:15")), [ord("A"), ord("B")])

    def test_slot_1130_batch_includes_earlier(self):
        # 11:30 시점에는 A~D 가 모두 대상이다(A·B 는 이미 처리됐다면 due 가 아니게 된다)
        self.assertEqual(self._ids(t("11:30")), [ord(x) for x in "ABCD"])


class CatchUp(unittest.TestCase):
    """Test 6 · 7 — 슬롯 누락과 데모 배속."""

    def test_missed_slot_is_picked_up_later(self):
        now = t("11:33")
        missed = stay(1, t("11:07"), now)   # 11:15 슬롯을 놓쳤다
        picked = select_stays([missed], slot_limit=current_slot(now))  # 11:30
        self.assertEqual([s["stay_id"] for s in picked], [1])

    def test_clock_jump_covers_every_passed_slot(self):
        # 데모 시계가 11:00 → 11:40 으로 튀어도 11:15·11:30 대상이 모두 들어온다
        now = t("11:40")
        stays = [stay(1, t("11:05"), now), stay(2, t("11:20"), now)]
        picked = select_stays(stays, slot_limit=current_slot(now))
        self.assertEqual([s["stay_id"] for s in picked], [1, 2])


class DemoAxis(unittest.TestCase):
    """슬롯은 화면(데모 축) 기준이다. demo_offset 은 15분 배수가 아니다."""

    def test_offset_shifts_slot(self):
        now = t("23:59")
        # 원본 축 11:07 + offset 8분 = 데모 축 11:15 → 11:15 슬롯 (올림 없음)
        s = stay(1, t("11:07"), now, offset=timedelta(minutes=8))
        self.assertEqual([x["stay_id"] for x in select_stays([s], slot_limit=t("11:15"))], [1])
        self.assertEqual(select_stays([s], slot_limit=t("11:00")), [])


class Horizon(unittest.TestCase):
    """감시 구간을 넘긴 환자는 헛호출하지 않는다."""

    def test_beyond_outtime_plus_offset_skipped(self):
        now = t("23:59")
        s = stay(1, t("11:07"), now, outtime=t("09:00"))  # 09:00 + 2h = 11:00 < 11:07
        self.assertEqual(select_stays([s], slot_limit=t("11:15"), outtime_offset_h=2), [])

    def test_inside_horizon_kept(self):
        now = t("23:59")
        s = stay(1, t("11:07"), now, outtime=t("10:00"))  # 10:00 + 2h = 12:00 > 11:07
        self.assertEqual(len(select_stays([s], slot_limit=t("11:15"), outtime_offset_h=2)), 1)


class ForceAll(unittest.TestCase):
    """all=true 는 예전 동작(전원 재계산)을 그대로 보존한다."""

    def test_force_all_ignores_due_and_slot(self):
        now = t("11:10")
        stays = [stay(1, t("11:07"), now), stay(2, t("23:00"), now)]
        self.assertEqual(len(select_stays(stays, force_all=True, slot_limit=t("11:00"))), 2)


if __name__ == "__main__":
    unittest.main()
