"""대시보드 집계 조회."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def summary_counts(db: Session) -> Any:
    sql = text("""
        SELECT
            count(*) FILTER (WHERE NOT d.has_departed)                 AS total,
            count(*) FILTER (WHERE d.has_departed)                     AS discharged,
            count(*) FILTER (WHERE NOT d.has_departed
                             AND lp.risk_level = 'critical')           AS critical,
            count(*) FILTER (WHERE NOT d.has_departed
                             AND lp.risk_level = 'rising')             AS rising,
            count(*) FILTER (WHERE NOT d.has_departed
                             AND lp.risk_level = 'watch')              AS watch,
            count(*) FILTER (WHERE NOT d.has_departed
                             AND lp.risk_level = 'stable')             AS stable,
            count(*) FILTER (WHERE NOT d.has_departed
                             AND lp.risk_level IS NULL)                AS unassessed
        FROM mimic.edstays e
        JOIN app.v_demo_stay d ON d.ed_stay_id = e.stay_id
        LEFT JOIN app.v_latest_prediction lp ON lp.stay_id = e.stay_id
        WHERE d.is_active
    """)
    return db.execute(sql).mappings().one()


def alerts_today(db: Session) -> int:
    sql = text("SELECT count(*) FROM app.alert WHERE alert_time::date = CURRENT_DATE")
    return int(db.execute(sql).scalar_one())


def has_predictions(db: Session) -> bool:
    """예측 데이터가 실제로 있는지. URL 설정 여부가 아니라 데이터로 판단한다."""
    return db.execute(text("SELECT 1 FROM app.prediction LIMIT 1")).first() is not None


def cohort_size(db: Session) -> int:
    return int(db.execute(text("SELECT count(*) FROM mimic.edstays")).scalar_one())


def list_beds(db: Session) -> list[Any]:
    sql = text("""
        SELECT b.bed_id, b.zone, b.sort_order,
               -- ba 가 아니라 dm 에서 가져온다. 퇴실한 환자는 dm 이 NULL 이 되어
               -- 병상이 비어 있는 것으로 판정된다.
               dm.ed_stay_id, ba.devices,
               al.display_name, e.gender,
               p.anchor_age, p.anchor_year, e.intime,
               lp.risk_level, lp.risk_probability, t.acuity
        FROM app.bed b
        -- 퇴실한 환자의 병상은 비어 있는 것으로 본다.
        -- (released_at 을 배치로 채우는 대신 조회 시점에 판정한다 — 데모 시간축이
        --  조회 시점 기준이라 저장값으로 고정할 수 없기 때문이다)
        LEFT JOIN app.bed_assignment ba ON ba.bed_id = b.bed_id AND ba.released_at IS NULL
        LEFT JOIN app.v_demo_stay dm ON dm.ed_stay_id = ba.ed_stay_id AND NOT dm.has_departed
        LEFT JOIN mimic.edstays  e  ON e.stay_id    = dm.ed_stay_id
        LEFT JOIN mimic.patients p  ON p.subject_id = e.subject_id
        LEFT JOIN mimic.triage   t  ON t.stay_id    = e.stay_id
        LEFT JOIN app.patient_alias      al ON al.ed_stay_id = e.stay_id
        LEFT JOIN app.v_latest_prediction lp ON lp.stay_id   = e.stay_id
        ORDER BY b.sort_order
    """)
    return list(db.execute(sql).mappings())


def list_alerts(db: Session, *, limit: int, since: str | None) -> list[Any]:
    sql = text("""
        SELECT a.id, a.ed_stay_id, a.alert_time, a.level, a.message,
               a.acknowledged_at, al.display_name
        FROM app.alert a
        LEFT JOIN app.patient_alias al ON al.ed_stay_id = a.ed_stay_id
        WHERE (CAST(:since AS timestamp) IS NULL OR a.alert_time >= CAST(:since AS timestamp))
        ORDER BY a.alert_time DESC
        LIMIT :limit
    """)
    return list(db.execute(sql, {"limit": limit, "since": since}).mappings())


def reassess_candidates(db: Session) -> list[Any]:
    """재평가 우선순위. 예측이 없으면 triage acuity 로 대체 판정한다."""
    sql = text("""
        SELECT e.stay_id, al.display_name, t.acuity,
               lp.risk_level, lp.risk_probability
        FROM mimic.edstays e
        JOIN app.v_demo_stay d ON d.ed_stay_id = e.stay_id
        JOIN mimic.triage  t ON t.stay_id    = e.stay_id
        LEFT JOIN app.patient_alias      al ON al.ed_stay_id = e.stay_id
        LEFT JOIN app.v_latest_prediction lp ON lp.stay_id   = e.stay_id
        WHERE d.is_active AND NOT d.has_departed
        ORDER BY lp.risk_probability DESC NULLS LAST, t.acuity ASC NULLS LAST
        LIMIT 20
    """)
    return list(db.execute(sql).mappings())
