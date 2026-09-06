"""모델 성능 지표 계산과 평가 오케스트레이션.

🔑 지표는 순수 Python 으로 계산한다. backend 에는 numpy·scikit-learn 이 없고,
   성능 화면 하나를 위해 의존성을 늘리지 않는다. 현재 규모(1천 행 미만)에서 충분하다.

🔑 무엇을 세고 무엇을 빼는가
    pending    관찰창이 아직 데모 시계상 도래하지 않음 → 어떤 지표에도 넣지 않는다
    censored   관측 경로가 없어 판정 불가 → 음성으로 세지 않고 지표에서 뺀다
    truncated  학습 grid 밖(첫 악화 이후) → 학습 분포에 없는 구간이라 지표에서 뺀다
    evaluated  위 셋을 뺀 나머지. 지표는 이것만으로 계산한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from sqlalchemy.orm import Session

from app.core.config import settings
from app.repositories import model_monitoring as repo
from app.repositories.ml_features import bundle
from app.services import model_monitoring_label as label

# --------------------------------------------------------------------- 지표


def average_precision(scores: Sequence[float], labels: Sequence[int]) -> float | None:
    """PR-AUC. 보정 확률 **원값**으로 계산한다(threshold 를 적용하지 않는다).

    scikit-learn 의 average_precision_score 와 같은 정의다 — 계단 보간이며
    사다리꼴 보간을 쓰지 않는다(낙관적으로 나온다).
    """
    positives = sum(labels)
    if not positives or positives == len(labels):
        return None
    ranked = sorted(zip(scores, labels), key=lambda pair: pair[0], reverse=True)

    ap = 0.0
    true_positive = 0
    seen = 0
    previous_recall = 0.0
    index = 0
    while index < len(ranked):
        score = ranked[index][0]
        # 같은 점수는 한 덩어리로 처리한다. 나누면 순서에 따라 값이 흔들린다.
        while index < len(ranked) and ranked[index][0] == score:
            true_positive += ranked[index][1]
            seen += 1
            index += 1
        recall = true_positive / positives
        precision = true_positive / seen
        ap += (recall - previous_recall) * precision
        previous_recall = recall
    return ap


def roc_auc(scores: Sequence[float], labels: Sequence[int]) -> float | None:
    """AUROC. 순위 기반(Mann-Whitney U)이며 동점은 평균 순위로 처리한다."""
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None

    ranked = sorted(zip(scores, labels), key=lambda pair: pair[0])
    ranks = [0.0] * len(ranked)
    index = 0
    while index < len(ranked):
        end = index
        while end + 1 < len(ranked) and ranked[end + 1][0] == ranked[index][0]:
            end += 1
        # 1-based 평균 순위
        average_rank = (index + end) / 2 + 1
        for position in range(index, end + 1):
            ranks[position] = average_rank
        index = end + 1

    rank_sum = sum(rank for rank, (_, y) in zip(ranks, ranked) if y)
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


@dataclass(frozen=True)
class Confusion:
    true_positive: int = 0
    false_positive: int = 0
    true_negative: int = 0
    false_negative: int = 0

    @property
    def recall(self) -> float | None:
        actual = self.true_positive + self.false_negative
        return self.true_positive / actual if actual else None

    @property
    def precision(self) -> float | None:
        flagged = self.true_positive + self.false_positive
        return self.true_positive / flagged if flagged else None

    @property
    def f1(self) -> float | None:
        recall, precision = self.recall, self.precision
        if recall is None or precision is None or (recall + precision) == 0:
            return None
        return 2 * recall * precision / (recall + precision)


def confusion_at(scores: Sequence[float], labels: Sequence[int], threshold: float) -> Confusion:
    """threshold 를 실제로 적용한다 — `risk_probability >= threshold` 가 양성 예측이다."""
    tp = fp = tn = fn = 0
    for score, actual in zip(scores, labels):
        predicted = score >= threshold
        if predicted and actual:
            tp += 1
        elif predicted:
            fp += 1
        elif actual:
            fn += 1
        else:
            tn += 1
    return Confusion(tp, fp, tn, fn)


def threshold_sweep(
    scores: Sequence[float], labels: Sequence[int], thresholds: Sequence[float]
) -> list[dict[str, Any]]:
    """운영점 후보별 성능.

    ⚠ PR-AUC·AUROC 는 넣지 않는다. 둘은 threshold 와 무관한 값이라
      운영점마다 다른 값처럼 보이면 오해를 부른다.
    """
    rows = []
    for threshold in thresholds:
        matrix = confusion_at(scores, labels, threshold)
        rows.append({
            "threshold": threshold,
            "recall": matrix.recall,
            "precision": matrix.precision,
            "f1": matrix.f1,
            "positive_predictions": matrix.true_positive + matrix.false_positive,
            "true_positive": matrix.true_positive,
            "false_positive": matrix.false_positive,
        })
    return rows


# --------------------------------------------------------------------- 평가 상태


@dataclass
class EvaluatedPrediction:
    ed_stay_id: int
    prediction_time: datetime           # MIMIC 원본 축
    demo_prediction_time: datetime      # 화면 축
    risk_probability: float
    threshold: float
    model_version: str
    evaluation_end_time: datetime
    observation_end: datetime | None
    outcome_label: int | None
    event_type: str | None
    event_time: datetime | None
    is_due: bool
    is_censored: bool
    is_truncated: bool

    @property
    def counts_toward_metrics(self) -> bool:
        return self.is_due and not self.is_censored and not self.is_truncated \
            and self.outcome_label is not None


@dataclass
class EvaluationState:
    rows: list[EvaluatedPrediction] = field(default_factory=list)
    label_definition: str = ""
    model_version: str = ""
    threshold: float = 0.0
    label_window_h: int = 3

    @property
    def evaluated(self) -> list[EvaluatedPrediction]:
        return [row for row in self.rows if row.counts_toward_metrics]

    @property
    def pending(self) -> list[EvaluatedPrediction]:
        return [row for row in self.rows if not row.is_due]

    @property
    def censored(self) -> list[EvaluatedPrediction]:
        return [row for row in self.rows if row.is_due and row.is_censored]

    @property
    def truncated(self) -> list[EvaluatedPrediction]:
        return [row for row in self.rows
                if row.is_due and not row.is_censored and row.is_truncated]


def _model_threshold(detail_threshold: float | None) -> float:
    """운영 threshold. 예측이 남긴 값이 먼저고, 없으면 bundle 의 기본 운영점이다."""
    if detail_threshold is not None:
        return float(detail_threshold)
    operating_point = bundle()["default_operating_point"]
    return float(bundle()["operating_points"][operating_point]["threshold"])


def evaluate(db: Session, *, apply_demo_gate: bool = True, persist: bool = True) -> EvaluationState:
    """예측 전체를 채점하고 결과를 app.model_outcome 에 캐시한다.

    별도 스케줄러를 두지 않는다 — 조회할 때 계산하고 upsert 한다(D6).

    🔑 `apply_demo_gate=False` 는 **라벨 검증 전용**이다.
       화면에서는 데모 시계상 관찰창이 끝난 예측만 평가해야 한다(그래야 시연에서
       "아직 결과를 모르는 시점" 이 재현된다). 하지만 라벨이 학습과 같은지 확인할 때는
       그 게이트가 방해만 된다 — MIMIC 원본 시간축에서는 모든 관찰창이 이미 끝났고,
       라벨의 정오는 데모 시계와 아무 상관이 없다.
       이 모드에서는 `persist=False` 로 두어 캐시를 오염시키지 않는다.
    """
    predictions = repo.list_predictions(db)
    grid = bundle()["grid"]
    window_h = int(grid["label_window_h"])

    stays: dict[int, dict[str, Any]] = {}
    hadm_to_stays: dict[int, list[int]] = {}
    for row in predictions:
        stay_id = int(row["ed_stay_id"])
        if stay_id in stays:
            continue
        stays[stay_id] = {
            "hadm_id": int(row["hadm_id"]) if row["hadm_id"] else None,
            "ed_intime": row["ed_intime"],
            "obs_end": row["obs_end"],
        }
        if row["hadm_id"]:
            hadm_to_stays.setdefault(int(row["hadm_id"]), []).append(stay_id)

    procedure_itemids = list(
        label.RESP_INVASIVE_ITEMIDS + label.RESP_NIV_ITEMIDS + label.CPR_ITEMIDS
    )
    itemid_map = label.chart_itemid_map(bundle()["vital_itemids"])

    onsets = label.abnormal_onsets(
        ed_vital_rows=repo.ed_vitalsigns(db),
        chart_vital_rows=repo.chart_vitalsigns(db, sorted(itemid_map)),
        stays=stays,
        hadm_to_stays=hadm_to_stays,
        itemid_map=itemid_map,
        valid_range=bundle()["valid_range"],
    )
    events = label.build_events(
        procedure_rows=repo.procedure_events(db, procedure_itemids),
        vasopressor_starts=label.vasopressor_events(
            infusion_rows=repo.vasopressor_infusions(db, list(label.VASOPRESSOR_ITEMIDS)),
            emar_rows=repo.emar_administrations(db),
            prescription_rows=repo.prescription_orders(db),
        ),
        hospital_death_rows=repo.hospital_deaths(db),
        ed_death_rows=repo.ed_deaths(db),
        onsets=onsets,
    )

    state = EvaluationState(label_window_h=window_h)
    cache_rows: list[dict[str, Any]] = []

    for row in predictions:
        stay_id = int(row["ed_stay_id"])
        hadm_id = int(row["hadm_id"]) if row["hadm_id"] else None
        prediction_time = row["prediction_time"]
        evaluation_end = prediction_time + timedelta(hours=window_h)
        threshold = _model_threshold(row["detail_threshold"])
        model_version = row["model_version"]
        if not state.model_version:
            state.model_version = model_version
            state.threshold = threshold
            state.label_definition = label.label_definition_id(model_version, window_h)

        is_due = bool(row["is_due"]) if apply_demo_gate else True
        # 관측 경로가 아예 없는 구간만 중도절단으로 본다.
        # 입원 기록이 없으면 처치·사망을 관측할 수단이 없고, 관찰창이 obs_end 를 넘으면
        # 활력징후도 더 이상 기록되지 않는다.
        # ⚠ hadm_id 가 없다는 것만으로 중도절단 처리하지 않는다. 학습도 그런 stay 를
        #   음성으로 뒀고(귀가·자의퇴원이 대부분), 그렇게 하지 않으면 코호트가 크게 왜곡된다.
        # ⚠ 데모 시계를 조건에 넣지 않는다. 중도절단은 "관측할 수단이 있었는가" 라는
        #   데이터의 성질이지 "언제 물어봤는가" 가 아니다. 시계를 섞으면 캐시에 남은
        #   is_censored 가 조회 시점에 따라 달라져 의미가 흔들린다.
        is_censored = (
            hadm_id is None
            and row["obs_end"] is not None and evaluation_end > row["obs_end"]
        )
        is_truncated = label.is_after_first_event(
            events=events, stay_id=stay_id, hadm_id=hadm_id, prediction_time=prediction_time,
        )

        outcome_label: int | None = None
        event_type: str | None = None
        event_time: datetime | None = None
        if is_due and not is_censored:
            outcome_label, event_type, event_time = label.evaluate_prediction(
                events=events, stay_id=stay_id, hadm_id=hadm_id,
                prediction_time=prediction_time, window_h=window_h,
            )

        state.rows.append(EvaluatedPrediction(
            ed_stay_id=stay_id,
            prediction_time=prediction_time,
            demo_prediction_time=row["demo_prediction_time"],
            risk_probability=float(row["risk_probability"]),
            threshold=threshold,
            model_version=model_version,
            evaluation_end_time=evaluation_end,
            observation_end=row["obs_end"],
            outcome_label=outcome_label,
            event_type=event_type,
            event_time=event_time,
            is_due=is_due,
            is_censored=is_censored,
            is_truncated=is_truncated,
        ))

        # 아직 도래하지 않은 예측은 캐시하지 않는다. 판정이 없는 행을 남기면
        # 데모 시계를 되감았을 때 "이미 평가된 것" 처럼 보인다.
        if is_due and persist:
            cache_rows.append({
                "ed_stay_id": stay_id,
                "prediction_time": prediction_time,
                "label_definition": state.label_definition,
                "evaluation_end_time": evaluation_end,
                "observation_end": row["obs_end"],
                "outcome_label": outcome_label,
                "event_type": event_type,
                "event_time": event_time,
                "is_censored": is_censored,
                "is_truncated": is_truncated,
            })

    if persist:
        repo.upsert_outcomes(db, cache_rows)
    return state


# --------------------------------------------------------------------- 판정


def _meets(value: float | None, target: float) -> bool | None:
    return None if value is None else value >= target


def status_of(matrix: Confusion, pr_auc: float | None, auroc: float | None,
              evaluated: int, positives: int) -> str:
    """화면 상태.

    🔑 판별력(PR-AUC·AUROC)이 목표 미달이면 critical 이다. 모델 자체의 신호다.
       Recall·Precision·F1 미달은 warning 으로 둔다 — 운영점(threshold)을 어디에 두느냐의
       함수이고, 프로젝트 문서도 Recall 목표 미달을 "모델 성능 자체의 문제가 아니다" 라고
       기록하고 있다(services/riskmodel/README.md 성능 절).
    """
    if evaluated < settings.model_min_eval_samples or positives < settings.model_min_positives:
        return "insufficient_data"
    if _meets(pr_auc, settings.model_target_pr_auc) is False:
        return "critical"
    if _meets(auroc, settings.model_target_auroc) is False:
        return "critical"
    for value, target in (
        (matrix.recall, settings.model_target_recall),
        (matrix.precision, settings.model_target_precision),
        (matrix.f1, settings.model_target_f1),
    ):
        if _meets(value, target) is False:
            return "warning"
    return "normal"


def metrics_for(rows: Sequence[EvaluatedPrediction], threshold: float) -> dict[str, Any]:
    """평가 완료 행만 받아 지표를 낸다. 표본이 모자라면 값은 None 이다 — 0 이 아니다."""
    scores = [row.risk_probability for row in rows]
    labels = [int(row.outcome_label or 0) for row in rows]
    matrix = confusion_at(scores, labels, threshold)
    pr_auc = average_precision(scores, labels)
    auroc = roc_auc(scores, labels)
    positives = sum(labels)
    return {
        "pr_auc": pr_auc,
        "auroc": auroc,
        "recall": matrix.recall,
        "precision": matrix.precision,
        "f1": matrix.f1,
        "true_positive": matrix.true_positive,
        "false_positive": matrix.false_positive,
        "true_negative": matrix.true_negative,
        "false_negative": matrix.false_negative,
        "threshold": threshold,
        "evaluated_count": len(rows),
        "positive_count": positives,
        "status": status_of(matrix, pr_auc, auroc, len(rows), positives),
    }


def record_snapshot(
    db: Session,
    state: EvaluationState,
    metrics: dict[str, Any],
) -> bool:
    """성능 이력을 남긴다. 평가 집합이 그대로면 적지 않는다(리포지토리가 판정).

    🔑 기간은 **MIMIC 원본 축**(prediction_time)으로 적는다. 데모 시각으로 적으면
       시계를 리셋할 때 epoch 이 재설정되어(app.demo_clock.epoch_virtual) 같은 평가
       결과인데도 기간 값이 달라진다 — 이력끼리 비교가 안 되고, 중복 방지 판정도
       기간을 보므로 리셋만 해도 같은 내용이 새 행으로 쌓인다.
       화면에 보여줄 일이 생기면 그때 조회 계층에서 demo_offset 을 더해 변환한다.
    """
    periods = [row.prediction_time for row in state.evaluated]
    if not periods:
        return False
    return repo.record_metric_snapshot(db, {
        "model_version": state.model_version,
        "label_definition": state.label_definition,
        "evaluation_period_start": min(periods),
        "evaluation_period_end": max(periods),
        "sample_count": len(state.rows),
        "evaluated_count": metrics["evaluated_count"],
        "pending_count": len(state.pending),
        "censored_count": len(state.censored),
        "positive_count": metrics["positive_count"],
        "pr_auc": metrics["pr_auc"],
        "auroc": metrics["auroc"],
        "recall": metrics["recall"],
        "precision": metrics["precision"],
        "f1": metrics["f1"],
        "true_positive": metrics["true_positive"],
        "true_negative": metrics["true_negative"],
        "false_positive": metrics["false_positive"],
        "false_negative": metrics["false_negative"],
        "threshold": metrics["threshold"],
    })


# 표본이 모자랄 때 값을 지우는 대상. 개수(TP/FP/TN/FN)는 남긴다 —
# 그건 추정값이 아니라 사실이고, "왜 못 냈는지" 를 설명해 준다.
_ESTIMATES = ("pr_auc", "auroc", "recall", "precision", "f1")


def redact_if_insufficient(metrics: dict[str, Any]) -> dict[str, Any]:
    """표본이 목표에 못 미치면 지표를 null 로 지운다.

    🔑 110건에 양성 2건으로 낸 PR-AUC 0.567 은 계산은 되지만 아무것도 말해주지 않는다.
       그런 숫자를 화면에 띄우면 "성능이 이 정도" 로 읽힌다. status 로만 경고하고 값을
       그대로 두면 화면 문구("산출하지 않습니다")와 응답이 서로 어긋난다.
    """
    if metrics["status"] != "insufficient_data":
        return metrics
    return {**metrics, **{key: None for key in _ESTIMATES}}


def label_coverage(state: EvaluationState) -> dict[str, Any]:
    """라벨을 어디까지 확보했는지. 모든 응답에 붙인다."""
    total = len(state.rows)
    evaluated = state.evaluated
    return {
        "total_predictions": total,
        "evaluated": len(evaluated),
        "pending": len(state.pending),
        "censored": len(state.censored),
        "truncated": len(state.truncated),
        "positive": sum(1 for row in evaluated if row.outcome_label),
        "label_definition": state.label_definition,
        # 학습 라벨을 그대로 재현했는지. 구성요소가 빠지면 운영 지표를 쓸 수 없다.
        "label_components_reproduced": list(label.MAIN_EVENT_TYPES),
        "coverage_percentage": round(100 * len(evaluated) / total, 2) if total else 0.0,
    }


# --------------------------------------------------------------------- 구간


def bucket_of(moment: datetime, interval: str) -> datetime:
    if interval == "day":
        return moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return moment.replace(minute=0, second=0, microsecond=0)


def in_window(rows: Sequence[EvaluatedPrediction],
              start: datetime | None, end: datetime | None) -> list[EvaluatedPrediction]:
    """화면 축(데모 시각) 기준으로 자른다. 사용자가 보는 시간과 같아야 한다."""
    picked = rows
    if start is not None:
        picked = [row for row in picked if row.demo_prediction_time >= start]
    if end is not None:
        picked = [row for row in picked if row.demo_prediction_time <= end]
    return list(picked)


def sweep_thresholds(operating_points: dict[str, Any], current: float) -> list[float]:
    """bundle 의 운영점 5종 + 현재 운영값. 임의의 격자를 만들지 않는다."""
    values = {round(float(point["threshold"]), 6) for point in operating_points.values()}
    values.add(round(current, 6))
    return sorted(values)


def reference_performance() -> dict[str, Any]:
    """bundle 의 개발단계 temporal holdout 성능.

    ⚠ 운영 성능과 절대 섞지 않는다. 이 artifact 자체를 직접 측정한 값도 아니다
      (bundle.performance_reference.note).
    """
    reference = bundle()["performance_reference"]
    return {
        "pr_auc": reference.get("pr_auc"),
        "auroc": reference.get("roc_auc"),
        "recall": reference.get("recall"),
        "precision": reference.get("precision"),
        "f1": reference.get("f1"),
        "source": reference.get("source"),
        "protocol": reference.get("protocol"),
        "note": reference.get("note"),
    }


def model_info() -> dict[str, Any]:
    """모델 메타데이터. 전부 bundle 에서 읽는다 — DB 에 복제하지 않는다(D2)."""
    data = bundle()
    training = data.get("training", {})
    operating_point = data["default_operating_point"]
    return {
        "model_version": data["version"],
        "algorithm": data["model"]["kind"] + " + " + data["calibrator"]["kind"],
        "feature_set": data["feature_set"],
        "feature_count": data["n_features"],
        "prediction_horizon_h": data["grid"]["label_window_h"],
        "threshold": float(data["operating_points"][operating_point]["threshold"]),
        "operating_point": operating_point,
        "eval_unit": data["eval_unit"],
        "training_period": training.get("period"),
        "training_mode": training.get("mode"),
        "protocol": data["performance_reference"].get("protocol"),
        "created": data.get("created"),
    }


def isfinite(value: float | None) -> float | None:
    """JSON 으로 나갈 수 없는 값(NaN·inf)을 None 으로 바꾼다."""
    if value is None:
        return None
    return value if math.isfinite(value) else None
