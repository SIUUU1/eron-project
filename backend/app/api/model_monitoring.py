"""AI 모델 성능 모니터링 API (읽기 전용 · 조회 시 판정 후 캐시).

🔑 운영 성능과 참조 성능을 절대 섞지 않는다.
   operational  = 실제 예측을 학습과 같은 악화 정의로 채점한 값
   reference    = bundle 의 개발단계 temporal holdout 값 (배포 artifact 를 잰 값이 아니다)

🔑 시각 파라미터는 **데모 시간축**이다. 화면에 보이는 시각과 같아야 하기 때문이다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_db
from app.repositories import demo_clock
from app.repositories import model_monitoring as repo
from app.repositories.ml_features import bundle
from app.schemas.ed.model_monitoring import (
    ConfusionMatrixResponse,
    EvaluationStatusResponse,
    LabelCoverage,
    MetricTargets,
    ModelInfo,
    ModelMonitoringSummary,
    OperationalMetrics,
    ReferenceMetrics,
    ThresholdRow,
    ThresholdsResponse,
    TrendPoint,
    TrendsResponse,
)
from app.services import ed as ed_svc
from app.services import model_monitoring as svc

router = APIRouter(prefix="/api/ed/model-monitoring", tags=["ED Model Monitoring"])

INTERVALS = {"hour", "day"}



def _meta(db: Session):
    return ed_svc.build_meta(repo.cohort_size(db), model_connected=repo.has_predictions(db))


def _targets() -> MetricTargets:
    return MetricTargets(
        pr_auc=settings.model_target_pr_auc,
        auroc=settings.model_target_auroc,
        recall=settings.model_target_recall,
        precision=settings.model_target_precision,
        f1=settings.model_target_f1,
        min_eval_samples=settings.model_min_eval_samples,
        min_positives=settings.model_min_positives,
    )


@router.get(
    "/summary",
    response_model=ModelMonitoringSummary,
    summary="최신 모델 성능",
    description=(
        "실제 예측을 **학습과 같은 악화 정의**로 채점한 운영 성능이다. "
        "관찰 3시간이 끝난 예측만 들어가며, 중도절단·학습 grid 밖 시점은 제외한다. "
        "표본이 부족하면 지표는 null 이고 status 는 insufficient_data 다 — 숫자를 지어내지 않는다. "
        "reference 는 개발 단계 temporal holdout 값으로 운영 성능이 아니다."
    ),
)
def get_summary(db: Session = Depends(get_db)) -> ModelMonitoringSummary:
    state = svc.evaluate(db)
    evaluated = state.evaluated
    metrics = svc.metrics_for(evaluated, state.threshold)
    demo_times = [row.demo_prediction_time for row in evaluated]
    # 이력에는 지우지 않은 값을 남긴다. 나중에 되짚을 때 원본이 있어야 한다.
    # ⚠ 이력의 기간은 MIMIC 원본 축이다. 아래 응답의 evaluation_period_* 는 화면용
    #   데모 축이며 서로 다른 축이다(저장은 안정적인 원본, 표시는 사용자가 보는 시간).
    svc.record_snapshot(db, state, metrics)
    # 화면에 나가는 것은 표본이 충분할 때만이다(§27 insufficient_data).
    metrics = svc.redact_if_insufficient(metrics)
    return ModelMonitoringSummary(
        model_info=ModelInfo(**svc.model_info()),
        operational=OperationalMetrics(**metrics),
        reference=ReferenceMetrics(**svc.reference_performance()),
        targets=_targets(),
        label_coverage=LabelCoverage(**svc.label_coverage(state)),
        evaluation_period_start=min(demo_times) if demo_times else None,
        evaluation_period_end=max(demo_times) if demo_times else None,
        last_evaluated_at=repo.last_computed_at(db, state.label_definition),
        meta=_meta(db),
    )


@router.get(
    "/trends",
    response_model=TrendsResponse,
    summary="성능 추이",
    description=(
        "구간별 성능. **평가가 끝난 예측만** 쓴다. "
        "start/end 는 데모 시간축이며 비우면 최근 24시간이다. "
        "구간의 표본이 모자라면 그 점의 지표는 null 이다(0 이 아니다)."
    ),
)
def get_trends(
    db: Session = Depends(get_db),
    start: datetime | None = Query(None, description="ISO 8601 · 데모 시간축"),
    end: datetime | None = Query(None, description="ISO 8601 · 데모 시간축"),
    interval: str = Query("hour", description="hour | day"),
) -> TrendsResponse:
    if interval not in INTERVALS:
        raise HTTPException(status_code=400, detail=f"interval must be one of {sorted(INTERVALS)}")

    state = svc.evaluate(db)
    if end is None:
        end = demo_clock.read(db)["virtual_now"]
    if start is None:
        start = end - timedelta(days=1 if interval == "hour" else 30)

    buckets: dict[datetime, list] = {}
    for row in svc.in_window(state.evaluated, start, end):
        buckets.setdefault(svc.bucket_of(row.demo_prediction_time, interval), []).append(row)

    points = []
    for bucket in sorted(buckets):
        rows = buckets[bucket]
        metrics = svc.metrics_for(rows, state.threshold)
        # 구간 표본이 목표에 못 미치면 판별력 지표는 신뢰할 수 없다 → null 로 둔다.
        usable = metrics["status"] != "insufficient_data"
        points.append(TrendPoint(
            bucket=bucket,
            pr_auc=metrics["pr_auc"] if usable else None,
            auroc=metrics["auroc"] if usable else None,
            recall=metrics["recall"],
            precision=metrics["precision"],
            f1=metrics["f1"],
            evaluated_count=metrics["evaluated_count"],
            positive_count=metrics["positive_count"],
        ))

    return TrendsResponse(
        interval=interval,
        points=points,
        label_coverage=LabelCoverage(**svc.label_coverage(state)),
        meta=_meta(db),
    )


@router.get(
    "/confusion-matrix",
    response_model=ConfusionMatrixResponse,
    summary="혼동 행렬",
    description=(
        "threshold 를 실제로 적용한 결과다(risk_probability >= threshold 가 양성 예측). "
        "threshold 를 주지 않으면 현재 운영값을 쓴다."
    ),
)
def get_confusion_matrix(
    db: Session = Depends(get_db),
    start: datetime | None = Query(None, description="ISO 8601 · 데모 시간축"),
    end: datetime | None = Query(None, description="ISO 8601 · 데모 시간축"),
    threshold: float | None = Query(None, ge=0.0, le=1.0),
) -> ConfusionMatrixResponse:
    state = svc.evaluate(db)
    rows = svc.in_window(state.evaluated, start, end)
    metrics = svc.redact_if_insufficient(
        svc.metrics_for(rows, threshold if threshold is not None else state.threshold))
    return ConfusionMatrixResponse(
        **{key: metrics[key] for key in (
            "threshold", "true_positive", "false_positive", "true_negative",
            "false_negative", "recall", "precision", "f1",
            "evaluated_count", "positive_count", "status",
        )},
        label_coverage=LabelCoverage(**svc.label_coverage(state)),
        meta=_meta(db),
    )


@router.get(
    "/thresholds",
    response_model=ThresholdsResponse,
    summary="운영점 분석",
    description=(
        "bundle 의 운영점 5종과 현재 운영값에서의 성능. "
        "PR-AUC·AUROC 는 threshold 와 무관한 값이라 넣지 않는다."
    ),
)
def get_thresholds(db: Session = Depends(get_db)) -> ThresholdsResponse:
    state = svc.evaluate(db)
    evaluated = state.evaluated
    operating_points = bundle()["operating_points"]
    names = {round(float(point["threshold"]), 6): name
             for name, point in operating_points.items()}

    scores = [row.risk_probability for row in evaluated]
    labels = [int(row.outcome_label or 0) for row in evaluated]
    candidates = svc.sweep_thresholds(operating_points, state.threshold)

    rows = []
    for entry in svc.threshold_sweep(scores, labels, candidates):
        value = round(entry["threshold"], 6)
        rows.append(ThresholdRow(
            **entry,
            is_current=value == round(state.threshold, 6),
            operating_point=names.get(value),
        ))

    return ThresholdsResponse(
        current_threshold=state.threshold,
        rows=rows,
        label_coverage=LabelCoverage(**svc.label_coverage(state)),
        meta=_meta(db),
    )


@router.get(
    "/status",
    response_model=EvaluationStatusResponse,
    summary="평가 진행 상태",
    description=(
        "예측 발생 후 3시간의 관찰 시간이 지나야 최종 평가가 가능하다. "
        "시각은 데모 시간축 기준이며, 도래 판정도 데모 시계로 한다."
    ),
)
def get_status(db: Session = Depends(get_db)) -> EvaluationStatusResponse:
    state = svc.evaluate(db)
    evaluated = state.evaluated
    metrics = svc.metrics_for(evaluated, state.threshold)
    demo_now = demo_clock.read(db)["virtual_now"]
    last_prediction = max((row.demo_prediction_time for row in state.rows), default=None)
    freshness = (
        round((demo_now - last_prediction).total_seconds() / 60, 1)
        if last_prediction is not None else None
    )
    return EvaluationStatusResponse(
        last_prediction_at=last_prediction,
        last_evaluated_at=repo.last_computed_at(db, state.label_definition),
        total_predictions=len(state.rows),
        evaluated_predictions=len(evaluated),
        pending_predictions=len(state.pending),
        censored_predictions=len(state.censored),
        truncated_predictions=len(state.truncated),
        latest_model_version=state.model_version or None,
        demo_now=demo_now,
        data_freshness_minutes=freshness,
        status=metrics["status"],
        label_coverage=LabelCoverage(**svc.label_coverage(state)),
        meta=_meta(db),
    )
