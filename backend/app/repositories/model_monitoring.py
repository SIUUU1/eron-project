"""모델 성능 모니터링 — DB 조회.

여기서는 SQL 만 담당한다. 악화 라벨 판정 규칙은
`app.services.model_monitoring_label` 한 곳에만 둔다.

🔑 시간축
    `app.prediction.prediction_time` 과 `mimic.*` 의 모든 시각은 **MIMIC 원본 축**이다.
    데모 시계(app.demo_now())는 "그 관찰창이 화면상 도래했는가" 만 판정한다.
    demo_offset 은 stay 마다 다르므로(app.v_demo_stay) 전역 now() 로 판정하면 안 된다.

🔑 관찰 종료(obs_end)는 `ml_features.py` 와 같은 정의를 쓴다(배치 build_cohort.py 이식본):
        base    = coalesce(ed_outtime + ed_outtime_offset_h, admittime + horizon_h)
        obs_end = min(base, dischtime, deathtime)
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.repositories.ml_features import bundle

_NO_END = "2300-01-01"


# --------------------------------------------------------------------- 예측 대상

# 예측 1건 + 판정에 필요한 맥락(관찰창·데모 도래 여부)을 한 번에 가져온다.
#
# 🔑 is_due 는 stay 별 demo_offset 을 적용한 결과다.
#    prediction_time + demo_offset + label_window <= demo_now()
_PREDICTIONS = text(f"""
    SELECT p.ed_stay_id,
           p.prediction_time,
           p.risk_probability,
           p.model_version,
           (p.detail->>'threshold')::double precision       AS detail_threshold,
           e.hadm_id,
           e.intime                                          AS ed_intime,
           (app.demo_epoch() - d.now_ref)                    AS demo_offset,
           p.prediction_time + (app.demo_epoch() - d.now_ref) AS demo_prediction_time,
           least(
               coalesce(e.outtime + make_interval(hours => CAST(:outtime_offset_h AS int)),
                        a.admittime + make_interval(hours => CAST(:horizon_h AS int)),
                        timestamp '{_NO_END}'),
               coalesce(a.dischtime, timestamp '{_NO_END}'),
               coalesce(a.deathtime, timestamp '{_NO_END}')
           )                                                  AS obs_end,
           (p.prediction_time
                + (app.demo_epoch() - d.now_ref)
                + make_interval(hours => CAST(:window_h AS int)) <= app.demo_now()
           )                                                  AS is_due
      FROM app.prediction p
      JOIN app.demo_stay d ON d.ed_stay_id = p.ed_stay_id
      JOIN mimic.edstays  e ON e.stay_id    = p.ed_stay_id
 LEFT JOIN mimic.admissions a ON a.hadm_id  = e.hadm_id
     ORDER BY p.ed_stay_id, p.prediction_time
""")


def list_predictions(db: Session) -> list[Any]:
    """평가 후보 예측 전체. 도래 여부(is_due)는 데모 시계로 판정해 함께 돌려준다."""
    cohort = bundle()["cohort"]
    grid = bundle()["grid"]
    return list(db.execute(_PREDICTIONS, {
        "outtime_offset_h": cohort["ed_outtime_offset_h"],
        "horizon_h": cohort["horizon_h"],
        "window_h": grid["label_window_h"],
    }).mappings())


# --------------------------------------------------------------------- 라벨 이벤트

def procedure_events(db: Session, itemids: list[int]) -> list[Any]:
    """호흡부전 처치 · CPR. hadm_id 로 조인한다."""
    sql = text("""
        SELECT hadm_id, itemid, starttime
          FROM mimic.procedureevents
         WHERE hadm_id IS NOT NULL AND itemid IN :itemids
    """).bindparams(bindparam("itemids", expanding=True))
    return list(db.execute(sql, {"itemids": itemids}).mappings())


def vasopressor_infusions(db: Session, itemids: list[int]) -> list[Any]:
    """승압제 주입행. 에피소드로 묶는 것은 라벨 모듈이 한다.

    정렬은 에피소드 판정 순서(icu stay · itemid · 시작시각)에 맞춘다.
    """
    sql = text("""
        SELECT hadm_id, icu_stay_id, itemid, starttime,
               coalesce(endtime, starttime) AS endtime
          FROM mimic.inputevents
         WHERE hadm_id IS NOT NULL AND itemid IN :itemids
         ORDER BY icu_stay_id, itemid, starttime
    """).bindparams(bindparam("itemids", expanding=True))
    return list(db.execute(sql, {"itemids": itemids}).mappings())


def emar_administrations(db: Session) -> list[Any]:
    """승압제 2순위 원천. 약물명 판정은 라벨 모듈이 한다."""
    return list(db.execute(text("""
        SELECT hadm_id, charttime, medication, event_txt
          FROM mimic.emar
         WHERE hadm_id IS NOT NULL AND medication IS NOT NULL
    """)).mappings())


def prescription_orders(db: Session) -> list[Any]:
    """승압제 3순위 원천."""
    return list(db.execute(text("""
        SELECT hadm_id, starttime, drug, route
          FROM mimic.prescriptions
         WHERE hadm_id IS NOT NULL AND drug IS NOT NULL
    """)).mappings())


def hospital_deaths(db: Session) -> list[Any]:
    """입원 중 사망. patients.dod 는 날짜 해상도라 쓰지 않는다."""
    return list(db.execute(text("""
        SELECT hadm_id, deathtime
          FROM mimic.admissions
         WHERE deathtime IS NOT NULL
    """)).mappings())


def ed_deaths(db: Session) -> list[Any]:
    """ED 사망. **stay_id 키다** — 대상 대부분이 hadm_id 를 갖지 않아 입원 기록으로는 못 잡는다."""
    return list(db.execute(text("""
        SELECT stay_id, outtime
          FROM mimic.edstays
         WHERE disposition = 'EXPIRED' AND outtime IS NOT NULL
    """)).mappings())


def ed_vitalsigns(db: Session) -> list[Any]:
    """생리학적 악화 onset 산출용 ED 활력징후 (원본값 · ℉ 그대로).

    `mimic.v_ed_vitalsign_clean` 을 쓰지 않는다. 그 view 는 범위·반올림 기준이
    배치와 달라(ml_features.py 주석과 같은 이유) 라벨이 어긋난다.
    """
    return list(db.execute(text("""
        SELECT v.stay_id, v.charttime,
               v.heartrate, v.resprate, v.o2sat, v.sbp, v.dbp, v.temperature
          FROM mimic.ed_vitalsign v
          JOIN app.cohort c ON c.ed_stay_id = v.stay_id
         ORDER BY v.stay_id, v.charttime
    """)).mappings())


def chart_vitalsigns(db: Session, itemids: list[int]) -> list[Any]:
    """ED 퇴실 후 구간을 메우는 ICU 활력징후. hadm_id 로 stay 에 붙인다."""
    sql = text("""
        SELECT hadm_id, charttime, itemid, valuenum
          FROM mimic.chartevents
         WHERE valuenum IS NOT NULL AND itemid IN :itemids
         ORDER BY hadm_id, charttime
    """).bindparams(bindparam("itemids", expanding=True))
    return list(db.execute(sql, {"itemids": itemids}).mappings())


# --------------------------------------------------------------------- 캐시

_UPSERT = text("""
    INSERT INTO app.model_outcome
        (ed_stay_id, prediction_time, label_definition, evaluation_end_time,
         observation_end, outcome_label, event_type, event_time,
         is_censored, is_truncated, computed_at)
    VALUES
        (:ed_stay_id, :prediction_time, :label_definition, :evaluation_end_time,
         :observation_end, :outcome_label, :event_type, :event_time,
         :is_censored, :is_truncated, now())
    ON CONFLICT (ed_stay_id, prediction_time, label_definition) DO UPDATE SET
        evaluation_end_time = EXCLUDED.evaluation_end_time,
        observation_end     = EXCLUDED.observation_end,
        outcome_label       = EXCLUDED.outcome_label,
        event_type          = EXCLUDED.event_type,
        event_time          = EXCLUDED.event_time,
        is_censored         = EXCLUDED.is_censored,
        is_truncated        = EXCLUDED.is_truncated,
        computed_at         = now()
""")


def upsert_outcomes(db: Session, rows: list[dict[str, Any]]) -> int:
    """판정 결과를 캐시한다. 같은 (stay, 시점, 라벨정의)면 덮어쓴다.

    라벨 정의가 다르면 다른 행이 되므로 과거 정의의 판정은 남는다.
    """
    if not rows:
        return 0
    db.execute(_UPSERT, rows)
    db.commit()
    return len(rows)


_LATEST_SNAPSHOT = text("""
    SELECT evaluated_count, positive_count, evaluation_period_end
      FROM app.model_performance_metric
     WHERE model_version = :model_version AND label_definition = :label_definition
     ORDER BY created_at DESC
     LIMIT 1
""")

_INSERT_SNAPSHOT = text("""
    INSERT INTO app.model_performance_metric
        (model_version, label_definition, evaluation_period_start, evaluation_period_end,
         sample_count, evaluated_count, pending_count, censored_count, positive_count,
         pr_auc, auroc, recall, precision, f1,
         true_positive, true_negative, false_positive, false_negative, threshold)
    VALUES
        (:model_version, :label_definition, :evaluation_period_start, :evaluation_period_end,
         :sample_count, :evaluated_count, :pending_count, :censored_count, :positive_count,
         :pr_auc, :auroc, :recall, :precision, :f1,
         :true_positive, :true_negative, :false_positive, :false_negative, :threshold)
""")


def record_metric_snapshot(db: Session, row: dict[str, Any]) -> bool:
    """성능 snapshot 을 남긴다 — **평가 집합이 달라졌을 때만.**

    🔑 조회할 때마다 적으면 30초 폴링에 하루 2,880 행이 쌓이는데 값은 대부분 같다.
       평가 완료 건수나 양성 건수가 달라진 순간만 기록하면, 모델 교체 전후나
       코호트 변화를 되돌아볼 수 있는 최소한의 이력이 남는다.

    돌려주는 값은 "이번에 실제로 적었는가" 다.
    """
    previous = db.execute(_LATEST_SNAPSHOT, {
        "model_version": row["model_version"],
        "label_definition": row["label_definition"],
    }).mappings().first()

    if previous is not None and (
        previous["evaluated_count"] == row["evaluated_count"]
        and previous["positive_count"] == row["positive_count"]
        and previous["evaluation_period_end"] == row["evaluation_period_end"]
    ):
        return False

    db.execute(_INSERT_SNAPSHOT, row)
    db.commit()
    return True


def last_computed_at(db: Session, label_definition: str) -> Any:
    return db.execute(text("""
        SELECT max(computed_at) FROM app.model_outcome
         WHERE label_definition = :label_definition
    """), {"label_definition": label_definition}).scalar()


def cohort_size(db: Session) -> int:
    return int(db.execute(text("SELECT count(*) FROM mimic.edstays")).scalar_one())


def has_predictions(db: Session) -> bool:
    return db.execute(text("SELECT 1 FROM app.prediction LIMIT 1")).first() is not None
