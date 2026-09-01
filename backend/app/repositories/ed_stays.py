"""ED stay 조회. 필요한 컬럼만 SELECT 하고 stay_id 인덱스로 접근한다.

stay 당 "최신 1건" 은 app.v_latest_* view 의 LATERAL 로 처리하므로
목록 조회에서 N+1 이 발생하지 않는다.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

# 목록/상세가 공유하는 조인. WHERE 절만 바꿔 쓴다.
_BASE_FROM = """
FROM mimic.edstays e
JOIN app.v_demo_stay      d  ON d.ed_stay_id = e.stay_id
JOIN mimic.triage       t  ON t.stay_id    = e.stay_id
JOIN mimic.patients     p  ON p.subject_id = e.subject_id
LEFT JOIN app.patient_alias      al ON al.ed_stay_id = e.stay_id
LEFT JOIN app.v_latest_prediction lp ON lp.stay_id   = e.stay_id
-- 재검토 필요 알림(도래한 red 예측)과 그 확인 여부를 환자 단위로 집계한다.
-- 🔑 데모 시각까지 도래한 예측만 센다. 시계를 되돌리면 미래 시점의 알림·확인기록이
--    빠지므로 과거 상태가 오염되지 않는다.
LEFT JOIN LATERAL (
    SELECT count(*)                                              AS alert_total,
           count(*) FILTER (WHERE ak.acknowledged_at IS NULL)    AS alert_unread
    FROM app.prediction pr
    -- ⏱ 확인기록은 데모 시각 기준으로 유효할 때만 센다. 시계를 되돌리면 그보다
    --    나중에 한 확인은 없는 것으로 보고, 다시 앞으로 가면 되살아난다.
    LEFT JOIN app.prediction_ack ak ON ak.ed_stay_id = pr.ed_stay_id
                                   AND ak.prediction_time = pr.prediction_time
                                   AND ak.acknowledged_demo_at <= app.demo_now()
    WHERE pr.ed_stay_id = e.stay_id
      AND pr.prediction_time + d.demo_offset <= app.demo_now()
      AND (pr.detail->>'alarm')::boolean
      AND pr.detail->>'band' = 'red'
      -- 🔑 최신 예측이 재검토 필요일 때만 버튼·✓ 대상이다(실시간 AI 경고와 같은 규칙).
      AND lp.detail->>'band' = 'red'
      AND (lp.detail->>'alarm')::boolean
) alert ON TRUE
LEFT JOIN app.v_latest_vitalsign  lv ON lv.stay_id   = e.stay_id
LEFT JOIN app.bed_assignment      ba ON ba.ed_stay_id = e.stay_id AND ba.released_at IS NULL
LEFT JOIN public.clinical_records cr ON cr.ed_stay_id = CAST(e.stay_id AS text)
"""

_FILTER = """
WHERE d.is_active
  AND (CAST(:risk_level AS text) IS NULL OR lp.risk_level = CAST(:risk_level AS text))
  AND (CAST(:acuity AS int)  IS NULL OR t.acuity = CAST(:acuity AS int))
  AND (
        CAST(:search AS text) IS NULL
        OR CAST(e.stay_id AS text) LIKE CAST(:search AS text) || '%'
        OR t.chiefcomplaint ILIKE '%' || CAST(:search AS text) || '%'
      )
"""

_SORTS = {
    # 예측이 없으면 확률이 NULL 이므로 acuity 를 2순위로 쓴다.
    "risk": "ORDER BY lp.risk_probability DESC NULLS LAST, t.acuity ASC NULLS LAST, d.demo_intime DESC",
    "arrival": "ORDER BY d.demo_intime DESC",
    # KTAS 균형 정렬 — 각 acuity 그룹에서 최신 내원 순으로 번호를 매기고
    # 그 번호를 1순위로 쓴다. 결과적으로 1,2,3,4,5,1,2,3,4,5 … 로 섞여
    # 첫 페이지에 5개 등급이 모두 나타난다.
    # 희소 등급(코호트 기준 acuity 4 는 8건, 5 는 2건)이 뒤로 밀리지 않게 하려는 목적이며,
    # 임상적 우선순위 정렬이 아니다.
    "acuity_mix": (
        "ORDER BY row_number() OVER (PARTITION BY t.acuity ORDER BY d.demo_intime DESC), "
        "t.acuity ASC, d.demo_intime DESC"
    ),
}

_LIST_COLUMNS = """
SELECT
    e.stay_id,
    e.gender,
    e.arrival_transport,
    e.disposition,
    d.demo_outtime,
    d.has_departed,
    EXISTS (SELECT 1 FROM mimic.icustays i WHERE i.hadm_id = e.hadm_id) AS icu_transferred,
    p.anchor_age,
    p.anchor_year,
    e.intime,
    d.demo_intime,
    d.demo_offset,
    t.acuity,
    t.chiefcomplaint,
    al.display_name,
    lp.risk_level,
    lp.risk_probability,
    -- 모델이 준 3구간(green/amber/red). 화면 목록의 '현재 위험도'가 이걸 쓴다.
    -- risk_level(4단계)은 .env RISK_* 경계이고, band 는 bundle.json 실측 경계다.
    lp.detail->>'band' AS risk_band,
    -- 재검토 필요 알림 수와 미확인 수. 버튼 활성·목록 ✓ 가 이 값을 쓴다.
    coalesce(alert.alert_total, 0)  AS alert_total,
    coalesce(alert.alert_unread, 0) AS alert_unread,
    lv.measured_at,
    lv.heartrate,
    lv.resprate,
    lv.sbp,
    lv.dbp,
    lv.o2sat,
    lv.temperature_c,
    ba.bed_id,
    cr.status AS record_status
"""


SORT_KEYS = frozenset(_SORTS)


def _filter_params(risk_level: str | None, acuity: int | None, search: str | None) -> dict[str, Any]:
    return {"risk_level": risk_level, "acuity": acuity, "search": search}


def count_stays(db: Session, *, risk_level: str | None, acuity: int | None,
                search: str | None) -> int:
    sql = text(f"SELECT count(*) {_BASE_FROM} {_FILTER}")
    return int(db.execute(sql, _filter_params(risk_level, acuity, search)).scalar_one())


def list_stays(db: Session, *, page: int, page_size: int, risk_level: str | None,
               acuity: int | None, search: str | None, sort: str) -> list[Any]:
    order = _SORTS.get(sort, _SORTS["risk"])
    sql = text(f"{_LIST_COLUMNS} {_BASE_FROM} {_FILTER} {order} LIMIT :limit OFFSET :offset")
    params = _filter_params(risk_level, acuity, search)
    params.update({"limit": page_size, "offset": (page - 1) * page_size})
    return list(db.execute(sql, params).mappings())


def get_stay(db: Session, stay_id: int) -> Any | None:
    sql = text(f"""
        SELECT
            e.stay_id, e.subject_id, e.gender, e.race, e.arrival_transport,
            e.disposition, e.hadm_id, e.intime, e.outtime,
            p.anchor_age, p.anchor_year,
            d.demo_intime, d.demo_offset,
            t.acuity, t.chiefcomplaint,
            t.heartrate  AS tri_hr,  t.resprate AS tri_rr,
            t.sbp        AS tri_sbp, t.dbp      AS tri_dbp,
            t.o2sat      AS tri_spo2,
            CASE WHEN t.temperature BETWEEN 90 AND 115
                 THEN round(((t.temperature - 32) / 1.8)::numeric, 1)::double precision END AS tri_temp_c,
            t.pain       AS tri_pain,
            al.display_name,
            lp.risk_level, lp.risk_probability,
            lp.detail->>'band' AS risk_band,
            coalesce(alert.alert_total, 0)  AS alert_total,
            coalesce(alert.alert_unread, 0) AS alert_unread,
            ba.bed_id,
            adm.admission_location,
            EXISTS (SELECT 1 FROM mimic.icustays i WHERE i.hadm_id = e.hadm_id) AS icu_transferred
        {_BASE_FROM}
        LEFT JOIN mimic.admissions adm ON adm.hadm_id = e.hadm_id
        WHERE e.stay_id = :stay_id
    """)
    row = db.execute(sql, {"stay_id": stay_id}).mappings().first()
    return row


def list_vitals(db: Session, stay_id: int, *, limit: int, order: str) -> list[Any]:
    direction = "ASC" if order == "asc" else "DESC"
    sql = text(f"""
        SELECT c.charttime, c.heartrate, c.resprate, c.sbp, c.dbp, c.o2sat,
               c.temperature_c, c.rhythm, c.pain, d.demo_offset
        FROM mimic.v_ed_vitalsign_clean c
        JOIN app.v_demo_stay d ON d.ed_stay_id = c.stay_id
        WHERE c.stay_id = :stay_id
          -- 데모 시계보다 나중 측정은 감춘다 (시계를 진행하면 하나씩 드러난다)
          AND c.charttime + d.demo_offset <= app.demo_now()
        ORDER BY c.charttime {direction}
        LIMIT :limit
    """)
    return list(db.execute(sql, {"stay_id": stay_id, "limit": limit}).mappings())


def list_predictions(db: Session, stay_id: int) -> list[Any]:
    sql = text("""
        SELECT pr.prediction_time, pr.t_idx, pr.horizon_minutes,
               pr.risk_probability, pr.risk_level, pr.model_version, pr.detail,
               d.demo_offset
        FROM app.prediction pr
        JOIN app.v_demo_stay d ON d.ed_stay_id = pr.ed_stay_id
        WHERE pr.ed_stay_id = :stay_id
          AND pr.prediction_time + d.demo_offset <= app.demo_now()
        ORDER BY pr.prediction_time ASC
    """)
    return list(db.execute(sql, {"stay_id": stay_id}).mappings())


# 같은 (stay, 모델버전, 예측시각) 은 한 행이다. 스케줄러가 겹쳐 돌아도 중복이 생기지
# 않도록 UNIQUE 제약(prediction_unique)에 기대어 upsert 한다.
# 재실행 시 값이 같으면 그대로, 입력이 늘어 값이 바뀌면 갱신된다.
_UPSERT_PREDICTION = text("""
    INSERT INTO app.prediction
        (ed_stay_id, model_version, prediction_time, t_idx, horizon_minutes,
         risk_probability, risk_level, detail)
    VALUES
        (:ed_stay_id, :model_version, :prediction_time, :t_idx, :horizon_minutes,
         :risk_probability, :risk_level, CAST(:detail AS jsonb))
    ON CONFLICT ON CONSTRAINT prediction_unique DO UPDATE
       SET t_idx            = EXCLUDED.t_idx,
           horizon_minutes  = EXCLUDED.horizon_minutes,
           risk_probability = EXCLUDED.risk_probability,
           risk_level       = EXCLUDED.risk_level,
           detail           = EXCLUDED.detail
""")


def upsert_predictions(db: Session, rows: list[dict[str, Any]]) -> int:
    """예측 결과를 app.prediction 에 기록한다. 반환은 기록한 행 수.

    ⚠ prediction_time 은 **MIMIC 원본 시간축**이다. 화면 시각(데모 축)으로 넣으면
      app.v_latest_prediction 의 `prediction_time + demo_offset <= demo_now()` 판정이
      깨져 예측이 보이지 않거나 미래 예측이 새어 나온다.
    """
    if not rows:
        return 0
    db.execute(_UPSERT_PREDICTION, rows)
    db.commit()
    return len(rows)


def stay_exists(db: Session, stay_id: int) -> bool:
    """조회 가능한 stay 인가 = 데모 예측 대상인가.

    ⚠ mimic.edstays 로 판정하면 안 된다. 그 테이블에는 예측 대상 밖의 과거 내원까지
      들어 있어(raw source), 코호트에 없는 stay_id 가 404 대신 빈 200 을 받게 된다.
    """
    sql = text("SELECT 1 FROM app.cohort WHERE ed_stay_id = :stay_id")
    return db.execute(sql, {"stay_id": stay_id}).first() is not None
