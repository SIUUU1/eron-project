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
               lp.risk_level, lp.risk_probability,
               lp.detail->>'band' AS risk_band,
               t.acuity
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


def list_current_stay_records(db: Session) -> list[Any]:
    """현재 재실 환자와 응급진료기록을 한 번에 조회한다."""
    sql = text("""
        SELECT e.stay_id,
               al.display_name,
               e.intime,
               cr.status AS record_status,
               cr.record_payload
        FROM mimic.edstays e
        JOIN app.v_demo_stay d ON d.ed_stay_id = e.stay_id
        LEFT JOIN app.patient_alias al ON al.ed_stay_id = e.stay_id
        LEFT JOIN public.clinical_records cr
               ON cr.ed_stay_id = CAST(e.stay_id AS text)
        WHERE d.is_active
          AND NOT d.has_departed
        ORDER BY e.intime ASC NULLS LAST, e.stay_id ASC
    """)
    return list(db.execute(sql).mappings())


# 🔔 재검토 필요 알림.
#
#   알림 1건 = **예측 1시점**이다(환자 1명이 아니다). 같은 환자라도 예측 시점이 다르면
#   각각 쌓인다 — 12:00 · 13:00 · 14:00 이 모두 red 면 알림 3건이다.
#
# 🔑 알림을 적재하지 않는다. app.prediction 에서 조회 시점에 파생하므로 polling·새로고침
#    으로 같은 알림이 중복 생성될 수 없다 — 같은 (stay, prediction_time) 은 UNIQUE 제약
#    으로 DB 에 한 행뿐이고, 그 행이 곧 알림 1건이다.
#
# 🔑 데모 시계 기준. 도래하지 않은 예측(미래)은 알림에도, 확인 판정에도 들어오지 않는다.
#    시계를 되돌리면 그 이후 시점의 확인기록이 **조인 대상에서 빠져** 과거 상태를
#    오염시키지 않는다.
#
# ⚠ 확인기록(app.prediction_ack)은 (stay, prediction_time) 로만 매칭한다.
#   모델의 alarm/band 는 건드리지 않는다.
_ALERT_ROWS = """
    SELECT pr.id,
           pr.ed_stay_id,
           pr.prediction_time,
           pr.prediction_time + d.demo_offset          AS alert_time,
           pr.risk_level                               AS level,
           pr.risk_probability,
           pr.detail->>'band'                          AS band,
           pr.detail->>'reason'                        AS reason,
           pr.detail->>'reason_type'                   AS reason_type,
           ak.acknowledged_at,
           al.display_name,
           row_number() OVER (
               PARTITION BY pr.ed_stay_id ORDER BY pr.prediction_time DESC
           )                                           AS recency
    FROM app.prediction pr
    JOIN app.v_demo_stay d ON d.ed_stay_id = pr.ed_stay_id
    -- 🔑 지금(데모 시각) 기준 그 환자의 **최신 예측**이 재검토 필요일 때만 알림이다.
    --    과거에 red 였어도 최신이 안정이면 화면에 남기지 않는다.
    JOIN app.v_latest_prediction lp ON lp.stay_id = pr.ed_stay_id
                                   AND lp.detail->>'band' = 'red'
                                   AND (lp.detail->>'alarm')::boolean
    LEFT JOIN app.patient_alias al ON al.ed_stay_id = pr.ed_stay_id
    -- ⏱ 확인기록은 **데모 시각 기준으로** 유효해야 한다. 시계를 되돌리면 그보다
    --    나중에 한 확인은 아직 하지 않은 것으로 본다(기록은 지우지 않는다).
    LEFT JOIN app.prediction_ack ak
           ON ak.ed_stay_id = pr.ed_stay_id
          AND ak.prediction_time = pr.prediction_time
          AND ak.acknowledged_demo_at <= app.demo_now()
    WHERE pr.prediction_time + d.demo_offset <= app.demo_now()
      AND NOT d.has_departed
      AND (pr.detail->>'alarm')::boolean
"""

# 화면이 "재검토 필요" 로 부르는 구간. 종 카운트·버튼 활성 조건이 모두 이 값을 쓴다.
REVIEW_BAND = "red"


def alerts_today(db: Session) -> int:
    """데모 시계 기준 '오늘' 발생한 재검토 필요 알림 건수(재실 환자)."""
    sql = text(f"""
        WITH alerts AS ({_ALERT_ROWS})
        SELECT count(*) FROM alerts
        WHERE band = :band AND alert_time::date = app.demo_now()::date
    """)
    return int(db.execute(sql, {"band": REVIEW_BAND}).scalar_one())


def unread_alert_count(db: Session) -> int:
    """종 아이콘 숫자 — 아직 확인하지 않은 재검토 필요 알림 **건수**.

    ⚠ 숫자를 저장해 두고 증감시키지 않는다. 매번 예측·확인기록에서 계산하므로
      polling 과 확인 요청이 겹쳐도 중복 차감이 생기지 않는다.
    """
    sql = text(f"""
        WITH alerts AS ({_ALERT_ROWS})
        SELECT count(*) FROM alerts
        WHERE band = :band AND acknowledged_at IS NULL
    """)
    return int(db.execute(sql, {"band": REVIEW_BAND}).scalar_one())


def acknowledge_stay(db: Session, stay_id: int, *, by: str | None = None) -> int:
    """그 환자의 **미확인 재검토 필요 알림을 한 번에** 확인 처리한다. 반환은 처리 건수.

    ⚠ 대상은 '지금 데모 시각까지 도래한' 알림뿐이다. 미래 시점 예측은 확인하지 않는다
      — 시계를 되돌린 뒤 다시 진행했을 때 그 시점 알림이 이미 확인된 것처럼 보이면 안 된다.
    ⚠ 다른 환자의 알림은 건드리지 않는다.
    ⚠ 확인 시각은 **데모 시각**으로 남긴다. 시계를 되돌리면 그보다 나중에 한 확인은
      무효가 되고(기록은 유지), 다시 앞으로 가면 되살아난다.
    ⚠ 대상이 이미 없으면 0 건을 돌려준다(멱등).
    """
    result = db.execute(
        text(f"""
            WITH alerts AS ({_ALERT_ROWS})
            INSERT INTO app.prediction_ack
                (ed_stay_id, prediction_time, acknowledged_demo_at, acknowledged_by)
            SELECT ed_stay_id, prediction_time, app.demo_now(), :by
            FROM alerts
            WHERE ed_stay_id = :stay_id
              AND band = :band
              AND acknowledged_at IS NULL
            -- 시계를 되돌렸다가 다시 확인하면 '언제 확인했는지'를 현재 데모 시각으로 갱신한다.
            ON CONFLICT (ed_stay_id, prediction_time) DO UPDATE
               SET acknowledged_demo_at = EXCLUDED.acknowledged_demo_at,
                   acknowledged_at      = now(),
                   acknowledged_by      = EXCLUDED.acknowledged_by
        """),
        {"stay_id": stay_id, "by": by, "band": REVIEW_BAND},
    )
    db.commit()
    return int(result.rowcount or 0)


def list_alerts(
    db: Session,
    *,
    limit: int,
    since: str | None,
    band: str | None = None,
    latest_only: bool = False,
) -> list[Any]:
    """재검토 필요 알림 목록 (최신순).

    · `latest_only=True`  — 환자당 **최신 알림 1건**. 응급실 현황의 실시간 AI 경고가 쓴다.
    · `latest_only=False` — 그 환자의 재검토 필요 예측 시점을 모두(누적). 종 알림 목록이 쓴다.

    정렬은 **미확인 → 확인됨** 순이고, 각 그룹 안에서 최신 알림이 먼저다.
    같은 시각이면 예측 행 id 내림차순으로 순서를 고정한다(같은 입력이면 같은 순서).

    두 경우 모두 **현재 최신 예측이 재검토 필요인 환자**만 나온다(_ALERT_ROWS 의 조인).
    ⚠ `since` 는 데모 시간축으로 비교한다(alert_time 이 데모 축이다).
    ⚠ 퇴실 환자·미도래 예측·미래 시점 확인기록은 _ALERT_ROWS 에서 이미 걸러진다.
    """
    sql = text(f"""
        WITH alerts AS ({_ALERT_ROWS})
        SELECT * FROM alerts
        WHERE (CAST(:band AS text) IS NULL OR band = CAST(:band AS text))
          AND (CAST(:since AS timestamp) IS NULL OR alert_time >= CAST(:since AS timestamp))
          AND (NOT CAST(:latest_only AS boolean) OR recency = 1)
        -- 정렬: ① 미확인 먼저 ② 각 그룹 안에서 최신순 ③ 동시각이면 예측 행 id 로 확정
        --   acknowledged_at 은 데모 시각 기준으로 유효한 확인만 채워지므로,
        --   시계를 되돌리면 그 알림이 다시 미확인 그룹(위쪽)으로 올라온다.
        ORDER BY (acknowledged_at IS NOT NULL), alert_time DESC, id DESC
        LIMIT :limit
    """)
    return list(
        db.execute(
            sql,
            {"limit": limit, "since": since, "band": band, "latest_only": latest_only},
        ).mappings()
    )


def reassess_candidates(db: Session) -> list[Any]:
    """재평가 우선순위. 예측이 없으면 triage acuity 로 대체 판정한다."""
    sql = text("""
        SELECT e.stay_id, al.display_name, t.acuity,
               lp.risk_level, lp.risk_probability,
               lp.detail->>'band' AS risk_band,
               ba.bed_id
        FROM mimic.edstays e
        JOIN app.v_demo_stay d ON d.ed_stay_id = e.stay_id
        JOIN mimic.triage  t ON t.stay_id    = e.stay_id
        LEFT JOIN app.patient_alias      al ON al.ed_stay_id = e.stay_id
        LEFT JOIN app.v_latest_prediction lp ON lp.stay_id   = e.stay_id
        -- 배정된 병상(현황판과 같은 기준: 아직 반납되지 않은 배정)
        LEFT JOIN app.bed_assignment ba ON ba.ed_stay_id = e.stay_id
                                       AND ba.released_at IS NULL
        WHERE d.is_active AND NOT d.has_departed
        ORDER BY lp.risk_probability DESC NULLS LAST, t.acuity ASC NULLS LAST
        LIMIT 20
    """)
    return list(db.execute(sql).mappings())
