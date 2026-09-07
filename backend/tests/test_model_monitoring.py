"""AI 모델 성능 모니터링 — 라벨 판정과 지표 계산 회귀 테스트.

    ./backend/run-tests.sh tests.test_model_monitoring -v

🔑 이 테스트가 지키는 것
   악화 라벨 정의는 학습 저장소에 있고 이 저장소에는 옮겨 적은 사본만 있다
   (services/model_monitoring_label.py). 값이 조용히 어긋나면 성능 숫자가 통째로
   틀리므로, 상수와 판정 규칙을 여기서 고정한다.

⚠ 여기서 지키지 못하는 것
   **학습 쪽 정의가 바뀌었는지는 알 수 없다.** 이 테스트는 사본이 스스로와
   일관적인지만 본다. 정답과의 대조는 DB 와 학습 산출물이 함께 있어야 하므로
   별도 스크립트가 한다 — `backend/scripts/verify_label_reproduction.py`.
   라벨을 손대거나 모델을 교체하면 그 스크립트를 반드시 돌린다.

   최근 대조(2026-09-04): 현 코호트의 app.prediction 을 이 구현으로 채점하고
   학습 산출물 cache/grid_labels_C.parquet 와 (stay_id, t) 로 맞춘 결과
   **865행 전부 일치 · 양성 27/27 일치**.
"""

import unittest
from datetime import datetime, timedelta

from app.services import model_monitoring_label as label
from app.services.model_monitoring import (
    Confusion,
    average_precision,
    confusion_at,
    redact_if_insufficient,
    roc_auc,
    threshold_sweep,
)


def t(hour: int, minute: int = 0) -> datetime:
    return datetime(2180, 7, 22, hour, minute)


def infusion(icu_stay: int, itemid: int, start: datetime, end: datetime,
             hadm: int = 100) -> dict:
    return {"icu_stay_id": icu_stay, "itemid": itemid, "hadm_id": hadm,
            "starttime": start, "endtime": end}


class LabelSpec(unittest.TestCase):
    """Test 1 — 라벨 상수. 학습 설정(src/config/cohort.yaml)에서 옮겨 적은 값이다."""

    def test_procedure_itemids(self):
        self.assertEqual(label.RESP_INVASIVE_ITEMIDS, (224385, 225792))
        self.assertEqual(label.RESP_NIV_ITEMIDS, (225794,))
        self.assertEqual(label.CPR_ITEMIDS, (225466,))

    def test_vasopressor_excludes_inotropes(self):
        """강심제 Dobutamine(221653)·Milrinone(221986)은 승압제가 아니다."""
        self.assertNotIn(221653, label.VASOPRESSOR_ITEMIDS)
        self.assertNotIn(221986, label.VASOPRESSOR_ITEMIDS)
        self.assertIn(221906, label.VASOPRESSOR_ITEMIDS)   # Norepinephrine
        self.assertEqual(len(label.VASOPRESSOR_ITEMIDS), 10)

    def test_abnormal_vital_criteria(self):
        self.assertEqual(label.ABNORMAL_VITAL_MIN_COUNT, 2)
        self.assertEqual(label.ABNORMAL_VITAL_CRITERIA["sbp"], (90, None))
        self.assertEqual(label.ABNORMAL_VITAL_CRITERIA["resp_rate"], (8, 30))
        self.assertEqual(label.ABNORMAL_VITAL_CRITERIA["heart_rate"], (40, 130))

    def test_label_definition_id_carries_spec(self):
        """정의가 바뀌면 식별자가 바뀌어야 기존 판정을 덮어쓰지 않는다."""
        self.assertEqual(label.label_definition_id("2.0.0", 3),
                         "training_2.0.0_labelB_srcC_win3h")


class VasopressorEpisodes(unittest.TestCase):
    """Test 2 — 주입행 → 에피소드."""

    def test_short_infusion_is_not_an_event(self):
        """지속 1시간 미만은 이벤트가 아니다(일회성 투여 배제)."""
        rows = [infusion(1, 221906, t(10), t(10, 30))]
        self.assertEqual(label.vasopressor_episode_starts(rows), [])

    def test_exactly_one_hour_counts(self):
        rows = [infusion(1, 221906, t(10), t(11))]
        self.assertEqual(label.vasopressor_episode_starts(rows), [(100, t(10))])

    def test_contiguous_rows_merge_into_one_episode(self):
        """gap 1시간 이내로 이어지면 한 에피소드다 — 시작 시각은 처음 것."""
        rows = [
            infusion(1, 221906, t(10), t(10, 30)),
            infusion(1, 221906, t(10, 30), t(11)),
            infusion(1, 221906, t(11), t(11, 30)),
        ]
        self.assertEqual(label.vasopressor_episode_starts(rows), [(100, t(10))])

    def test_gap_over_one_hour_starts_new_episode(self):
        rows = [
            infusion(1, 221906, t(10), t(11)),
            infusion(1, 221906, t(14), t(16)),   # 3시간 비었다 → 새 에피소드
        ]
        self.assertEqual(label.vasopressor_episode_starts(rows),
                         [(100, t(10)), (100, t(14))])

    def test_different_itemid_is_a_separate_episode(self):
        rows = [
            infusion(1, 221906, t(10), t(12)),
            infusion(1, 222315, t(10), t(12)),
        ]
        self.assertEqual(len(label.vasopressor_episode_starts(rows)), 2)


class VasopressorSourcePriority(unittest.TestCase):
    """Test 3 — 3단 우선순위(source_version=C). hadm 별로 하나의 원천만 쓴다."""

    def setUp(self):
        self.infusions = [infusion(1, 221906, t(10), t(12), hadm=100)]
        self.emar = [{"hadm_id": 100, "charttime": t(9),
                      "medication": "Norepinephrine", "event_txt": "Started"},
                     {"hadm_id": 200, "charttime": t(9),
                      "medication": "Levophed", "event_txt": "Administered"}]
        self.prescriptions = [{"hadm_id": 100, "starttime": t(8),
                               "drug": "Norepinephrine", "route": "IV DRIP"},
                              {"hadm_id": 200, "starttime": t(8),
                               "drug": "Vasopressin", "route": "IV"},
                              {"hadm_id": 300, "starttime": t(8),
                               "drug": "Phenylephrine", "route": "IV BOLUS"}]

    def test_highest_available_source_wins(self):
        events = label.vasopressor_events(
            infusion_rows=self.infusions, emar_rows=self.emar,
            prescription_rows=self.prescriptions)
        by_hadm = {hadm: moment for hadm, moment in events}
        self.assertEqual(by_hadm[100], t(10))   # inputevents 가 있으므로 그것만
        self.assertEqual(by_hadm[200], t(9))    # emar 로 내려감
        self.assertEqual(by_hadm[300], t(8))    # prescriptions 까지 내려감
        self.assertEqual(len(events), 3)        # 같은 hadm 을 두 번 세지 않는다

    def test_inotropes_are_filtered_by_name(self):
        events = label.vasopressor_events(
            infusion_rows=[],
            emar_rows=[{"hadm_id": 400, "charttime": t(9),
                        "medication": "Dobutamine", "event_txt": "Started"}],
            prescription_rows=[])
        self.assertEqual(events, [])

    def test_non_start_emar_event_is_ignored(self):
        events = label.vasopressor_events(
            infusion_rows=[],
            emar_rows=[{"hadm_id": 400, "charttime": t(9),
                        "medication": "Norepinephrine", "event_txt": "Stopped"}],
            prescription_rows=[])
        self.assertEqual(events, [])

    def test_non_iv_prescription_route_is_ignored(self):
        events = label.vasopressor_events(
            infusion_rows=[], emar_rows=[],
            prescription_rows=[{"hadm_id": 400, "starttime": t(8),
                                "drug": "Norepinephrine", "route": "PO"}])
        self.assertEqual(events, [])


class AbnormalOnset(unittest.TestCase):
    """Test 4 — 생리학적 악화 onset. **전환**만 이벤트다."""

    def setUp(self):
        self.stays = {1: {"hadm_id": None, "ed_intime": t(0), "obs_end": t(23)}}
        self.valid_range = {"heart_rate": [10, 300], "sbp": [30, 300],
                            "spo2": [30, 100], "resp_rate": [1, 90],
                            "temperature": [25, 45]}

    def onsets(self, rows):
        return label.abnormal_onsets(
            ed_vital_rows=rows, chart_vital_rows=[], stays=self.stays,
            hadm_to_stays={}, itemid_map={}, valid_range=self.valid_range)

    def vital(self, moment, **values):
        row = {"stay_id": 1, "charttime": moment, "heartrate": None, "resprate": None,
               "o2sat": None, "sbp": None, "dbp": None, "temperature": None}
        row.update(values)
        return row

    def test_one_abnormal_is_not_enough(self):
        """min_count = 2 다. 하나만 이상이면 이벤트가 아니다."""
        rows = [self.vital(t(1), sbp=80, heartrate=80)]
        self.assertEqual(self.onsets(rows), [])

    def test_two_abnormal_triggers_onset(self):
        rows = [self.vital(t(1), sbp=80, o2sat=85)]
        self.assertEqual(self.onsets(rows), [(1, t(1))])

    def test_sustained_abnormal_reports_only_the_transition(self):
        """계속 비정상인 것은 이벤트가 아니다 — 맞혀도 예측이 아니다."""
        rows = [self.vital(t(1), sbp=80, o2sat=85),
                self.vital(t(2), sbp=78, o2sat=84),
                self.vital(t(3), sbp=75, o2sat=83)]
        self.assertEqual(self.onsets(rows), [(1, t(1))])

    def test_recovery_then_relapse_is_a_second_onset(self):
        rows = [self.vital(t(1), sbp=80, o2sat=85),
                self.vital(t(2), sbp=120, o2sat=98),
                self.vital(t(3), sbp=80, o2sat=85)]
        self.assertEqual(self.onsets(rows), [(1, t(1)), (1, t(3))])

    def test_values_carry_forward(self):
        """LOCF — 직전 관측값이 유지된다. 두 값이 다른 시각에 와도 동시 충족으로 본다."""
        rows = [self.vital(t(1), sbp=80),
                self.vital(t(2), o2sat=85)]
        self.assertEqual(self.onsets(rows), [(1, t(2))])

    def test_measurement_outside_observation_window_is_ignored(self):
        rows = [self.vital(t(0) - timedelta(hours=1), sbp=80, o2sat=85)]
        self.assertEqual(self.onsets(rows), [])

    def test_impossible_value_is_discarded(self):
        """생리학적 범위 밖 값은 측정 자체가 없던 것으로 본다."""
        rows = [self.vital(t(1), sbp=5, o2sat=85)]   # sbp 5 는 범위 밖
        self.assertEqual(self.onsets(rows), [])

    def test_ed_temperature_is_converted_to_celsius(self):
        """ED vitalsign 의 체온은 ℉ 다. 95℉ = 35℃ 로 기준(35.5℃) 미만이 된다."""
        rows = [self.vital(t(1), temperature=95.0, o2sat=85)]
        self.assertEqual(self.onsets(rows), [(1, t(1))])


class EvaluationWindow(unittest.TestCase):
    """Test 5 — (t, t+3h] 좌개·우폐 구간."""

    def setUp(self):
        self.events = label.Events()
        self.events.add_hadm(100, t(13), "resp_inv")

    def evaluate(self, moment):
        return label.evaluate_prediction(
            events=self.events, stay_id=1, hadm_id=100,
            prediction_time=moment, window_h=3)

    def test_event_inside_window_is_positive(self):
        self.assertEqual(self.evaluate(t(11))[0], 1)

    def test_event_exactly_at_window_end_is_positive(self):
        """우폐 — t+3h 는 포함한다."""
        self.assertEqual(self.evaluate(t(10))[0], 1)

    def test_event_exactly_at_prediction_time_is_negative(self):
        """좌개 — t 자신은 포함하지 않는다(이미 일어난 일이다)."""
        self.assertEqual(self.evaluate(t(13))[0], 0)

    def test_event_after_window_is_negative(self):
        self.assertEqual(self.evaluate(t(9, 59))[0], 0)

    def test_returns_event_type_and_time(self):
        outcome, kind, moment = self.evaluate(t(11))
        self.assertEqual((outcome, kind, moment), (1, "resp_inv", t(13)))

    def test_earliest_event_is_reported(self):
        self.events.add_stay(1, t(12), "abnormal")
        _, kind, moment = self.evaluate(t(11))
        self.assertEqual((kind, moment), ("abnormal", t(12)))

    def test_stay_keyed_events_do_not_need_hadm(self):
        """생리학적 악화·ED 사망은 hadm_id 가 없어도 판정된다."""
        events = label.Events()
        events.add_stay(1, t(13), "abnormal")
        outcome, kind, _ = label.evaluate_prediction(
            events=events, stay_id=1, hadm_id=None, prediction_time=t(11), window_h=3)
        self.assertEqual((outcome, kind), (1, "abnormal"))

    def test_hadm_events_do_not_leak_to_other_stays(self):
        """처치는 hadm_id 키, 생리학적 악화는 stay_id 키다. 섞으면 교차 배정된다."""
        outcome, _, _ = label.evaluate_prediction(
            events=self.events, stay_id=2, hadm_id=None, prediction_time=t(11), window_h=3)
        self.assertEqual(outcome, 0)


class Truncation(unittest.TestCase):
    """Test 6 — 학습 grid 는 첫 악화에서 끊긴다."""

    def setUp(self):
        self.events = label.Events()
        self.events.add_hadm(100, t(13), "vaso")

    def after(self, moment):
        return label.is_after_first_event(
            events=self.events, stay_id=1, hadm_id=100, prediction_time=moment)

    def test_before_first_event_is_kept(self):
        self.assertFalse(self.after(t(12)))

    def test_exactly_at_first_event_is_kept(self):
        """경계는 포함이다 — 학습 산출물과 대조해 확인했다."""
        self.assertFalse(self.after(t(13)))

    def test_after_first_event_is_excluded(self):
        self.assertTrue(self.after(t(14)))

    def test_no_event_means_nothing_is_truncated(self):
        self.assertFalse(label.is_after_first_event(
            events=label.Events(), stay_id=1, hadm_id=100, prediction_time=t(14)))


class Metrics(unittest.TestCase):
    """Test 7 — 지표 계산. 순수 Python 구현이 표준 정의와 같은지 본다."""

    def test_perfect_ranking(self):
        scores = [0.9, 0.8, 0.2, 0.1]
        labels = [1, 1, 0, 0]
        self.assertAlmostEqual(roc_auc(scores, labels), 1.0)
        self.assertAlmostEqual(average_precision(scores, labels), 1.0)

    def test_inverted_ranking(self):
        scores = [0.9, 0.8, 0.2, 0.1]
        labels = [0, 0, 1, 1]
        self.assertAlmostEqual(roc_auc(scores, labels), 0.0)

    def test_auroc_handles_ties_with_average_rank(self):
        """전부 같은 점수면 판별력이 없다 = 0.5."""
        self.assertAlmostEqual(roc_auc([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0]), 0.5)

    def test_average_precision_known_value(self):
        """1등이 양성, 3등이 양성 → AP = (1/2)(1) + (1/2)(2/3)."""
        scores = [0.9, 0.8, 0.7, 0.6]
        labels = [1, 0, 1, 0]
        self.assertAlmostEqual(average_precision(scores, labels), 0.5 + 1 / 3)

    def test_metrics_are_none_without_both_classes(self):
        """양성만 또는 음성만 있으면 값을 만들지 않는다 — 0 이 아니라 null 이다."""
        self.assertIsNone(roc_auc([0.9, 0.8], [1, 1]))
        self.assertIsNone(average_precision([0.9, 0.8], [0, 0]))

    def test_confusion_applies_threshold_inclusively(self):
        """운영 규칙은 risk_probability >= threshold 다."""
        matrix = confusion_at([0.5, 0.5, 0.4], [1, 0, 1], 0.5)
        self.assertEqual((matrix.true_positive, matrix.false_positive,
                          matrix.true_negative, matrix.false_negative), (1, 1, 0, 1))

    def test_confusion_derived_rates(self):
        matrix = Confusion(true_positive=3, false_positive=1,
                           true_negative=5, false_negative=1)
        self.assertAlmostEqual(matrix.recall, 0.75)
        self.assertAlmostEqual(matrix.precision, 0.75)
        self.assertAlmostEqual(matrix.f1, 0.75)

    def test_rates_are_none_when_undefined(self):
        """양성 예측이 하나도 없으면 precision 은 0 이 아니라 정의되지 않는다."""
        self.assertIsNone(Confusion().precision)
        self.assertIsNone(Confusion().recall)

    def test_threshold_sweep_excludes_ranking_metrics(self):
        """PR-AUC·AUROC 는 threshold 와 무관하다 — sweep 에 넣으면 오해를 부른다."""
        rows = threshold_sweep([0.9, 0.1], [1, 0], [0.5])
        self.assertNotIn("pr_auc", rows[0])
        self.assertNotIn("auroc", rows[0])
        self.assertEqual(rows[0]["positive_predictions"], 1)


if __name__ == "__main__":
    unittest.main()


class InsufficientDataRedaction(unittest.TestCase):
    """Test 9 — 표본이 모자라면 지표를 지운다. status 만 바꾸고 숫자를 남기면 안 된다."""

    def metrics(self, status: str) -> dict:
        return {"status": status, "pr_auc": 0.9, "auroc": 0.9, "recall": 0.9,
                "precision": 0.9, "f1": 0.9, "true_positive": 2, "false_positive": 1,
                "true_negative": 100, "false_negative": 0, "evaluated_count": 110,
                "positive_count": 2, "threshold": 0.035838}

    def test_estimates_are_removed(self):
        out = redact_if_insufficient(self.metrics("insufficient_data"))
        for key in ("pr_auc", "auroc", "recall", "precision", "f1"):
            self.assertIsNone(out[key], key)

    def test_counts_survive(self):
        """개수는 추정값이 아니라 사실이다 — 왜 못 냈는지를 설명해 준다."""
        out = redact_if_insufficient(self.metrics("insufficient_data"))
        self.assertEqual(out["true_positive"], 2)
        self.assertEqual(out["evaluated_count"], 110)
        self.assertEqual(out["positive_count"], 2)

    def test_sufficient_sample_is_untouched(self):
        for status in ("normal", "warning", "critical"):
            out = redact_if_insufficient(self.metrics(status))
            self.assertEqual(out["pr_auc"], 0.9, status)
