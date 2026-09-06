"""AI 모델 성능 모니터링 응답 스키마.

🔑 운영 성능과 참조 성능을 타입에서부터 분리한다.
   `OperationalMetrics` = 실제 예측을 채점한 값
   `ReferenceMetrics`   = bundle 의 개발단계 temporal holdout 값 (모델 담당자 제공)
   섞이면 화면에서 구분할 방법이 없어진다.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.ed.common import Meta

STATUS_DESCRIPTION = (
    "normal(목표 충족) | warning(운영점 지표 미달) | "
    "critical(판별력 미달) | insufficient_data(표본 부족)"
)


class LabelCoverage(BaseModel):
    """라벨을 어디까지 확보했는지. 성능 숫자를 믿어도 되는지의 근거다."""

    total_predictions: int
    evaluated: int = Field(..., description="관찰창이 끝나 판정된 예측 수. 지표는 이것만 쓴다.")
    pending: int = Field(..., description="관찰 3시간이 아직 지나지 않아 판정할 수 없는 예측 수")
    censored: int = Field(..., description="관측 경로가 없어 판정 불가. 음성으로 세지 않는다.")
    truncated: int = Field(
        ..., description="첫 악화 이후 시점. 학습 grid 밖이라 지표에서 제외한다."
    )
    positive: int = Field(..., description="평가 완료분 중 실제 악화가 발생한 예측 수")
    label_definition: str
    label_components_reproduced: list[str] = Field(
        ..., description="재현한 악화 이벤트 종류. 학습 정의와 같아야 운영 지표를 쓸 수 있다."
    )
    coverage_percentage: float


class OperationalMetrics(BaseModel):
    """실제 예측을 채점한 성능. **평가가 완료된 예측만** 들어간다."""

    pr_auc: float | None = Field(None, description="보정 확률 원값 기준. 표본 부족 시 null")
    auroc: float | None = None
    recall: float | None = None
    precision: float | None = None
    f1: float | None = None
    true_positive: int = 0
    false_positive: int = 0
    true_negative: int = 0
    false_negative: int = 0
    threshold: float
    evaluated_count: int
    positive_count: int
    status: str = Field(..., description=STATUS_DESCRIPTION)


class ReferenceMetrics(BaseModel):
    """개발 단계 temporal holdout 성능 — **참조용**.

    ⚠ 운영 성능이 아니다. 배포 artifact 자체를 직접 측정한 값도 아니다.
    """

    pr_auc: float | None = None
    auroc: float | None = None
    recall: float | None = None
    precision: float | None = None
    f1: float | None = None
    source: str | None = None
    protocol: str | None = None
    note: str | None = None


class ModelInfo(BaseModel):
    """artifacts/bundle.json 이 정본이다. DB 에 복제하지 않는다."""

    model_version: str
    algorithm: str
    feature_set: str
    feature_count: int
    prediction_horizon_h: int
    threshold: float
    operating_point: str
    eval_unit: str
    training_period: str | None = None
    training_mode: str | None = None
    protocol: str | None = None
    created: str | None = None


class MetricTargets(BaseModel):
    pr_auc: float
    auroc: float
    recall: float
    precision: float
    f1: float
    min_eval_samples: int
    min_positives: int


class ModelMonitoringSummary(BaseModel):
    model_info: ModelInfo
    operational: OperationalMetrics
    reference: ReferenceMetrics
    targets: MetricTargets
    label_coverage: LabelCoverage
    evaluation_period_start: datetime | None = None
    evaluation_period_end: datetime | None = None
    last_evaluated_at: datetime | None = None
    meta: Meta


class TrendPoint(BaseModel):
    bucket: datetime = Field(..., description="구간 시작 (데모 시간축)")
    pr_auc: float | None = None
    auroc: float | None = None
    recall: float | None = None
    precision: float | None = None
    f1: float | None = None
    evaluated_count: int
    positive_count: int


class TrendsResponse(BaseModel):
    interval: str = Field(..., description="hour | day")
    points: list[TrendPoint]
    label_coverage: LabelCoverage
    meta: Meta


class ConfusionMatrixResponse(BaseModel):
    threshold: float
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int
    recall: float | None = None
    precision: float | None = None
    f1: float | None = None
    evaluated_count: int
    positive_count: int
    status: str = Field(..., description=STATUS_DESCRIPTION)
    label_coverage: LabelCoverage
    meta: Meta


class ThresholdRow(BaseModel):
    threshold: float
    recall: float | None = None
    precision: float | None = None
    f1: float | None = None
    positive_predictions: int
    true_positive: int
    false_positive: int
    is_current: bool = Field(False, description="현재 운영 threshold 인지")
    operating_point: str | None = Field(None, description="bundle 의 운영점 이름")


class ThresholdsResponse(BaseModel):
    current_threshold: float
    rows: list[ThresholdRow]
    label_coverage: LabelCoverage
    meta: Meta


class EvaluationStatusResponse(BaseModel):
    last_prediction_at: datetime | None = Field(None, description="데모 시간축")
    last_evaluated_at: datetime | None = Field(None, description="판정을 계산한 실제 시각")
    total_predictions: int
    evaluated_predictions: int
    pending_predictions: int
    censored_predictions: int
    truncated_predictions: int
    latest_model_version: str | None = None
    demo_now: datetime | None = None
    data_freshness_minutes: float | None = Field(
        None, description="데모 시각과 마지막 예측 시점의 차이(분)"
    )
    status: str = Field(..., description=STATUS_DESCRIPTION)
    label_coverage: LabelCoverage
    meta: Meta
