"""FastAPI 래퍼 — 악화 예측 모델 C 를 내부 마이크로서비스로 노출한다.

이 파일과 `backend/app/repositories/ml_features.py` 만 새로 작성한 코드다.
`predict_service_with_reason_v3.py` · `online_features.py` · `calibrators.py` 는 배치
파이프라인과 위험도 오차 0 으로 일치 검증된 코드이므로 호출만 하고 로직을 재구현하지
않는다. 설명 문장도 마찬가지로 `reason_engine_v3.py` 것을 그대로 넘긴다.

🔑 파일명을 바꾸지 않는다. 모델 담당자가 주는 이름 그대로 둬야
   `check_v3_setup.py` 가 짝을 검사할 수 있고, 다음 전달본과 diff 가 깨지지 않는다.
(⚠ 일치 검증 대상은 feature·위험도다. 설명 문장은 검증 대상이 아니다.)

    GET  /health   아티팩트 무결성 · feature 수 · 운영점
    GET  /info     모델 메타 (bundle.json 요약)
    POST /predict  원본 관측 → 1시간 간격 위험도 + 기여 신호(설명)

🔑 왜 원본 관측(vital·lab·triage)을 그대로 받는가
    feature 100개를 만드는 규칙(시간창·slope 의 ddof·결측 처리)은 배치와 한 글자도
    달라지면 안 된다. backend 쪽에서 feature 를 만들어 보내면 그 규칙이 두 곳에
존재하게 된다. 그래서 backend 는 DB 에서 원본 관측만 꺼내 보내고,
    feature 생성은 이 서비스 안의 OnlineFeatureBuilder 한 곳에서만 한다.

🔑 왜 t 하나만 받지 않고 그리드를 통째로 계산하는가
    feature 중 `abnormal_vital_persistence` · `abnormal_vital_delta` 는 **이전 시점들의
    이상 vital 개수 이력**에 의존한다(online_features.build 의 prev_abn).
    임의의 t 한 점만 계산하면 이 값이 배치와 어긋난다 — 에러 없이 값만 틀린다.
    따라서 항상 ED 도착 + start_offset_h 부터 step_h 간격으로 재생(replay)한 뒤
    필요한 시점을 돌려준다.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

from riskmodel.online_features import OnlineFeatureBuilder
from riskmodel.predict_service_with_reason_v3 import RiskService

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("riskmodel.service")

ART_DIR = Path(os.getenv("ARTIFACTS_DIR", "/app/artifacts"))

# 관측 한 건 = (시각, 변수명, 값). 배치·온라인 빌더가 쓰는 형태 그대로다.
Observation = tuple[datetime, str, float | None]


class Patient(BaseModel):
    stay_id: int
    subject_id: int | None = None
    age: float | None = None
    ed_intime: datetime
    ed_outtime: datetime | None = None
    # 배치의 관측 종료 시각. min(ed_outtime + offset, dischtime, deathtime) 이며
    # dischtime·deathtime 은 DB 쪽에만 있으므로 backend 가 계산해 보낸다.
    # 없으면 ed_outtime + offset 으로 폴백한다.
    obs_end: datetime | None = None
    admittime: datetime | None = None
    chiefcomplaint: str | None = None

    # ⚠ triage_temperature 는 ℃ 다. MIMIC 원본은 ℉ 이므로 호출부에서 변환해 보낸다.
    triage_temperature: float | None = None
    triage_heartrate: float | None = None
    triage_resprate: float | None = None
    triage_o2sat: float | None = None
    triage_sbp: float | None = None
    triage_dbp: float | None = None
    # ⚠ triage_pain 은 원본이 자유텍스트('unable' 등)다. 숫자만 뽑아 보낸다.
    triage_pain: float | None = None
    triage_acuity: float | None = None


class PredictRequest(BaseModel):
    patient: Patient
    # charttime <= t 인 vital. 값 범위 clip 은 빌더가 한다(배치와 같은 기준).
    vitals: list[Observation] = Field(default_factory=list)
    # ⚠ storetime(결과 보고 시각) <= t 인 lab. charttime(채혈)이 아니다.
    labs: list[Observation] = Field(default_factory=list)
    # 이 시각까지의 그리드를 계산한다. 보통 '지금'(데모 시계) 이다.
    t_end: datetime
    # true 면 마지막 한 시점만 돌려준다. 그리드 재생은 어차피 처음부터 한다.
    only_last: bool = False


class ReasonSignal(BaseModel):
    """설명 한 줄. v3 부터 **model feature 1개** 단위다(그룹 합산이 아니다)."""

    feature: str
    feature_label: str
    text: str
    # 현재 위험 신호(current_risk_signal)
    value: float | None = None
    contribution: float | None = None
    # 직전 대비 상승 신호(risk_increase_signal)
    previous_value: float | None = None
    current_value: float | None = None
    previous_contribution: float | None = None
    current_contribution: float | None = None
    delta_contribution: float | None = None
    # ⚠ contribution 단위는 LightGBM raw-score SHAP 이다. 보정 확률의 %p 가 아니다.
    contribution_space: str | None = None
    # 임상 방향 gate 결과. 악화 신호로 노출된 항목은 항상 worsening 이다.
    clinical_direction: str | None = None
    clinical_rule: str | None = None
    clinical_gate_passed: bool | None = None
    previous_clinical_severity: float | None = None
    current_clinical_severity: float | None = None


class PredictionPoint(BaseModel):
    t: datetime
    t_idx: int
    risk: float
    risk_pct: float
    band: str
    alarm: bool
    # 직전 시점 대비 확률 변화(보정 확률 기준). 그 stay 의 첫 시점이면 None.
    risk_delta: float | None = None
    risk_delta_pct_point: float | None = None
    # ⚠ 인과적 '악화 원인'이 아니라 모델 예측에 기여한 주요 신호다.
    #   risk_increase_signal = 직전 대비 상승에 기여한 Δcontribution 상위 feature
    #   current_risk_signal  = 현재 위험도에 기여한 contribution 상위 feature
    reason: str | None = None
    # current_risk_signal | risk_increase_clinical_worsening_signal
    # | risk_increase_without_confirmed_clinical_worsening_signal
    #   ⚠ 마지막 값은 "위험도는 올랐지만 임상적 악화로 확인된 변화가 없다" 는 뜻이며
    #     reason_detail 이 **비어 있는 것이 정상**이다. 비었다고 설명 없음으로 취급하면 안 된다.
    reason_type: str | None = None
    # 화면 제목. 모델이 만든 문구를 그대로 쓴다.
    reason_title: str | None = None
    reason_basis: str | None = None
    # 위험 상승 시점에서 임상 방향 gate 를 통과한 악화 변화가 있었는가.
    clinical_worsening_confirmed: bool | None = None
    reason_detail: list[ReasonSignal] = Field(default_factory=list)


class PredictResponse(BaseModel):
    stay_id: int
    model_version: str
    operating_point: str
    threshold: float
    window_h: int
    # 적용 범위 밖이면 false 이고 predictions 는 비어 있다. 확률을 지어내지 않는다.
    in_scope: bool
    out_of_scope_reason: str | None = None
    # 설명을 화면에 띄울 때 반드시 함께 표시해야 하는 문구. 설명이 없으면 None.
    reason_notice: str | None = None
    # 임상 방향 gate 버전. 어떤 규칙으로 걸러진 설명인지 되짚을 수 있어야 한다.
    clinical_gate_version: str | None = None
    service_explainability_version: str | None = None
    predictions: list[PredictionPoint] = Field(default_factory=list)


app = FastAPI(
    title="ER:ON RiskModel",
    description="ED 내원 성인 환자의 악화 예측 (모델 C · production refit)",
    version="1.0.0",
)

# 34MB 모델을 요청마다 읽지 않도록 프로세스 기동 시 1회만 로드한다.
_builder = OnlineFeatureBuilder(art_dir=ART_DIR)
_service = RiskService(art_dir=ART_DIR)
_bundle = _service.b

# 적용 범위·그리드는 bundle.json 이 정본이다. 코드에 상수로 박지 않는다.
_MIN_AGE = _bundle["cohort"]["min_age"]
_OUTTIME_OFFSET_H = _bundle["cohort"]["ed_outtime_offset_h"]
_START_OFFSET_H = _bundle["grid"]["start_offset_h"]
_STEP_H = _bundle["grid"]["step_h"]

logger.info(
    "loaded artifacts=%s version=%s n_features=%d op=%s threshold=%.6f "
    "grid=+%dh/step %dh scope=age>=%s, ED+%dh~퇴실+%dh",
    ART_DIR, _bundle["version"], len(_service.features), _service.op_name,
    _service.threshold, _START_OFFSET_H, _STEP_H, _MIN_AGE,
    _START_OFFSET_H, _OUTTIME_OFFSET_H,
)


@app.get("/health")
def health() -> dict:
    """아티팩트 무결성 + 필수 파일 존재 확인. RiskService.health() 를 그대로 쓴다."""
    return _service.health()


@app.get("/info")
def info() -> dict:
    """모델 메타. 성능 수치는 bundle.json 의 performance_reference 를 그대로 전달한다.

    ⚠ 이 값은 동일 고정 사양의 temporal holdout 결과이며 배포 artifact 자체를
      직접 측정한 값이 아니다. 화면·문서에 쓸 때 반드시 병기해야 한다.
    """
    return dict(
        version=_bundle["version"],
        created=_bundle["created"],
        task=_bundle["task"],
        target=_bundle["target"],
        n_features=_bundle["n_features"],
        feature_set=_bundle["feature_set"],
        feature_hash=_bundle["feature_hash"],
        operating_point=_service.op_name,
        threshold=_service.threshold,
        risk_bands=_bundle["risk_bands"]["thresholds"],
        window_h=_service.window_h,
        scope=dict(
            min_age=_MIN_AGE,
            start_offset_h=_START_OFFSET_H,
            step_h=_STEP_H,
            ed_outtime_offset_h=_OUTTIME_OFFSET_H,
        ),
        performance_reference=_bundle["performance_reference"],
    )


def _scope_violation(p: Patient, t_end: datetime) -> str | None:
    """적용 범위 밖이면 사유를, 안이면 None 을 돌려준다.

    성인(18세 이상) · ED 도착 +start_offset_h ~ ED 퇴실 +ed_outtime_offset_h.
    나이를 모르면 성인임을 확인할 수 없으므로 범위 밖으로 본다.
    """
    if p.age is None:
        return "age_unknown"
    if p.age < _MIN_AGE:
        return f"age_below_{_MIN_AGE}"
    if t_end < p.ed_intime + timedelta(hours=_START_OFFSET_H):
        return f"before_ed_intime_plus_{_START_OFFSET_H}h"
    return None


def _grid_end(p: Patient, t_end: datetime) -> datetime:
    """마지막 예측 시점의 상한 = 배치의 obs_end.

    배치(`src/data/build_cohort.py`)는
        obs_end = min(ed_outtime + ed_outtime_offset_h, dischtime, deathtime)
    로 관측을 끊는다. 퇴원·사망이 ED 퇴실 +offset 보다 이르면 그쪽이 상한이다.
    backend 가 obs_end 를 보내면 그대로 쓰고, 없으면 ED 퇴실 +offset 으로 폴백한다.
    """
    if p.obs_end is not None:
        return min(t_end, p.obs_end)
    if p.ed_outtime is None:
        return t_end
    return min(t_end, p.ed_outtime + timedelta(hours=_OUTTIME_OFFSET_H))


def _point(row: dict) -> PredictionPoint:
    """채점 결과 한 행 → 응답 한 점.

    ⚠ reason_detail 이 비어도 reason_type·reason_title 은 그대로 넘긴다.
      임상 방향 gate 가 도입되면서 "위험도는 올랐지만 확인된 악화 신호가 없다" 는
      상태가 생겼고, 그 상태는 빈 목록 + 제목으로 표현된다.
    """
    detail = [ReasonSignal(**x) for x in row.get("reason_detail") or []]
    return PredictionPoint(
        t=_as_datetime(row["t"]),
        t_idx=row["t_idx"],
        risk=row["risk"],
        risk_pct=row["risk_pct"],
        band=row["band"],
        alarm=bool(row["alarm"]),
        risk_delta=row.get("risk_delta"),
        risk_delta_pct_point=row.get("risk_delta_pct_point"),
        reason=row.get("reason") or None,
        reason_type=row.get("reason_type"),
        reason_title=row.get("reason_title"),
        reason_basis=row.get("reason_basis"),
        clinical_worsening_confirmed=row.get("clinical_worsening_confirmed"),
        reason_detail=detail,
    )


def _as_datetime(value) -> datetime:
    """score_series_with_reason 은 t 를 문자열로 돌려준다. 원래 시각으로 되돌린다."""
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _score(frame) -> list[dict]:
    """시간순 위험도 + 설명. 설명이 실패해도 위험도는 반드시 돌려준다.

    설명은 LightGBM `pred_contrib` 에 기대므로 모델 종류가 바뀌면 여기만 깨진다.
    그때 예측 전체를 멈추면 화면의 위험도까지 사라지므로, 경고만 남기고 위험도만
    돌려준다 — 위험도 값 자체는 두 경로가 같은 `_proba` 를 쓰므로 동일하다.
    """
    try:
        return _service.score_series_with_reason(frame)
    except Exception:  # noqa: BLE001
        logger.exception("설명 생성 실패 — 위험도만 반환한다")
        return list(_service.score_rows(frame).iter_rows(named=True))


@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest) -> PredictResponse:
    p = req.patient
    base = dict(
        stay_id=p.stay_id,
        model_version=_bundle["version"],
        operating_point=_service.op_name,
        threshold=_service.threshold,
        window_h=_service.window_h,
    )

    reason = _scope_violation(p, req.t_end)
    if reason is not None:
        return PredictResponse(in_scope=False, out_of_scope_reason=reason, **base)

    t0 = p.ed_intime + timedelta(hours=_START_OFFSET_H)
    last = _grid_end(p, req.t_end)
    if last < t0:
        # 퇴실 +offset 이 첫 예측 시점보다 이르다 = 감시 구간이 존재하지 않는다.
        return PredictResponse(
            in_scope=False,
            out_of_scope_reason=f"after_ed_outtime_plus_{_OUTTIME_OFFSET_H}h",
            **base,
        )

    n_steps = int((last - t0).total_seconds() // (3600 * _STEP_H)) + 1

    # 🔑 항상 t0 부터 재생한다. prev_abn 이력이 배치와 같아야 하기 때문이다.
    frame = _builder.build_series(
        dict(
            stay_id=p.stay_id,
            age=p.age,
            ed_intime=p.ed_intime,
            admittime=p.admittime,
            chiefcomplaint=p.chiefcomplaint or "",
            triage_temperature=p.triage_temperature,
            triage_heartrate=p.triage_heartrate,
            triage_resprate=p.triage_resprate,
            triage_o2sat=p.triage_o2sat,
            triage_sbp=p.triage_sbp,
            triage_dbp=p.triage_dbp,
            triage_pain=p.triage_pain,
            triage_acuity=p.triage_acuity,
        ),
        req.vitals,
        req.labs,
        t0,
        n_steps,
        step_h=_STEP_H,
    )
    scored = _score(frame)
    if req.only_last:
        scored = scored[-1:]

    return PredictResponse(
        in_scope=True,
        reason_notice=next((r["reason_notice"] for r in scored if r.get("reason_notice")), None),
        clinical_gate_version=next(
            (r["clinical_gate_version"] for r in scored if r.get("clinical_gate_version")), None
        ),
        service_explainability_version=next(
            (r["service_explainability_version"] for r in scored
             if r.get("service_explainability_version")), None
        ),
        predictions=[_point(row) for row in scored],
        **base,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8790")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
