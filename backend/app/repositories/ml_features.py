"""DB → 악화 예측 모델 입력 어댑터.

`services/riskmodel` 이 feature 100개를 만들려면 원본 관측(triage·vital·lab)이 필요하다.
여기서는 그 원본을 DB 에서 꺼내 배치 파이프라인과 **같은 형태**로 정리만 한다.
feature 계산은 하지 않는다 — 그 규칙은 riskmodel 안 OnlineFeatureBuilder 한 곳에만 있다.

🔴 배치와 어긋나면 에러 없이 성능만 떨어진다. 아래는 전부 실측으로 확정된 규칙이다.

  · vital  `ed_intime <= charttime <= min(t, obs_end)`
  · lab    `t0 - 24h <= avail_time <= min(t, t1)`,
           **avail_time = coalesce(storetime, charttime)**
           (charttime 은 채혈 시각이라 결과 보고보다 중앙 0.90h 이르다 = 미래 정보)
  · triage 시간 제한 없음 (ED 도착 시 확정)
  · 관찰창은 배치 `src/data/extract_{vitals,labs}.py` 와 같은 정의다
  · itemid·feature 목록은 `artifacts/bundle.json` 이 정본. 코드에 하드코딩하지 않는다.
  · 단위 변환 2건 — `ed_vitalsign.temperature`(℉) · chartevents `223761`(℉)
  · 값 범위 clip 은 **여기서 하지 않는다.** 모델의 valid_range 로 빌더가 처리한다.
    (`mimic.v_ed_vitalsign_clean` 은 범위·반올림 기준이 달라 배치와 어긋난다 → 원본 테이블을 읽는다)

시간축 주의 — `mimic.*` 와 `app.prediction` 은 **MIMIC 원본 시간축**이다.
데모 시계(app.demo_now())는 조회 시점에 demo_offset 을 더해 만든 화면용 축이므로,
모델에 넘기는 `t` 는 반드시 원본 축이어야 한다. `list_stays_for_prediction()` 이 변환해 준다.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

ARTIFACTS_DIR = Path(os.getenv("ARTIFACTS_DIR", "/app/artifacts"))

# ED vitalsign 컬럼 → 모델 변수명
_ED_VITAL_COLUMNS = {
    "heartrate": "heart_rate",
    "resprate": "resp_rate",
    "o2sat": "spo2",
    "sbp": "sbp",
    "dbp": "dbp",
}

_PAIN_DIGITS = re.compile(r"(\d+)")

# lab 관찰창의 하한 여유. 배치 `src/config/features.yaml derived.lab_window_h` 와 같은 값이며
# 서빙 코드 `riskmodel/online_features.py` 의 LAB_WINDOW_H 와도 같다.
# bundle.json 에는 실려 있지 않아 여기에 둔다 — 바꾸면 배치와 어긋난다.
LAB_WINDOW_H = 24

# obs_end 계산에서 "종료 없음" 을 나타내는 상한. 배치의 fill_null(2300-01-01) 과 같다.
_NO_END = "2300-01-01"


def _f_to_c(fahrenheit: float) -> float:
    """℉ → ℃. 반올림하지 않는다 — 배치가 반올림하지 않기 때문이다."""
    return (fahrenheit - 32) * 5 / 9


@lru_cache(maxsize=1)
def bundle() -> dict[str, Any]:
    """artifacts/bundle.json. feature 순서·itemid·운영점의 정본이다."""
    return json.loads((ARTIFACTS_DIR / "bundle.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _chartevents_itemids() -> dict[int, tuple[str, bool]]:
    """itemid → (모델 변수명, ℉→℃ 변환 필요 여부).

    bundle 은 온도를 `temperature_f` / `temperature_c` 두 키로 나눠 두었다.
    둘 다 모델 변수 `temperature` 이고, `_f` 쪽만 변환한다.
    """
    out: dict[int, tuple[str, bool]] = {}
    for key, itemids in bundle()["vital_itemids"].items():
        if key.endswith("_f"):
            var, needs_convert = key[:-2], True
        elif key.endswith("_c"):
            var, needs_convert = key[:-2], False
        else:
            var, needs_convert = key, False
        for itemid in itemids:
            out[int(itemid)] = (var, needs_convert)
    return out


@lru_cache(maxsize=1)
def _lab_itemids() -> dict[int, str]:
    """itemid → lab 변수명."""
    return {int(itemid): var for var, itemid in bundle()["lab_itemids"].items()}


# --------------------------------------------------------------------- patient

# 관찰창(obs_end)과 subject 단위 lab 창(t0·t1)을 한 번에 계산한다.
#
# 배치(`src/data/build_cohort.py`)의 obs_end 정의를 그대로 옮긴 것이다:
#   base    = coalesce(ed_outtime + ed_outtime_offset_h, admittime + horizon_h)
#   obs_end = min(base, dischtime, deathtime)     -- 없는 값은 2300-01-01 로 취급
#
# t0·t1 은 배치 `src/data/extract_labs.py` 의 span 과 같다:
#   t0 = 그 환자의 **모든 ED 내원** 중 최소 intime, t1 = 그 stay 들의 최대 obs_end
# 🔑 그래서 mimic.edstays 가 코호트(app.cohort)가 아니라 전체 ED 이력을 담아야 한다.
_STAY_OBS_CTE = """
WITH stay_obs AS (
    SELECT e.stay_id, e.subject_id, e.intime,
           least(
               coalesce(e.outtime + make_interval(hours => CAST(:outtime_offset_h AS int)),
                        a.admittime + make_interval(hours => CAST(:horizon_h AS int))),
               coalesce(a.dischtime, TIMESTAMP '""" + _NO_END + """'),
               coalesce(a.deathtime, TIMESTAMP '""" + _NO_END + """')
           ) AS obs_end
    FROM mimic.edstays e
    LEFT JOIN mimic.admissions a ON a.hadm_id = e.hadm_id
)
"""

_PATIENT_SQL = text(_STAY_OBS_CTE + """
    SELECT e.stay_id, e.subject_id, e.hadm_id,
           e.intime  AS ed_intime,
           e.outtime AS ed_outtime,
           so.obs_end,
           span.t0, span.t1,
           p.anchor_age, p.anchor_year,
           t.chiefcomplaint,
           t.temperature AS triage_temperature_f,
           t.heartrate   AS triage_heartrate,
           t.resprate    AS triage_resprate,
           t.o2sat       AS triage_o2sat,
           t.sbp         AS triage_sbp,
           t.dbp         AS triage_dbp,
           t.pain        AS triage_pain_text,
           t.acuity      AS triage_acuity
    FROM mimic.edstays e
    JOIN mimic.patients p ON p.subject_id = e.subject_id
    JOIN stay_obs so ON so.stay_id = e.stay_id
    JOIN LATERAL (
        SELECT min(s2.intime) AS t0, max(s2.obs_end) AS t1
        FROM stay_obs s2
        WHERE s2.subject_id = e.subject_id
    ) span ON TRUE
    LEFT JOIN mimic.triage t ON t.stay_id = e.stay_id
    WHERE e.stay_id = :stay_id
""")


def _pain_number(raw: str | None) -> float | None:
    """triage.pain 은 'unable' · '7/10' 같은 자유텍스트다. 숫자만 뽑는다.

    배치는 `pl.col("pain").str.extract(r"(\\d+)")` 를 쓴다. 같은 규칙이다.
    """
    if raw is None:
        return None
    found = _PAIN_DIGITS.search(raw)
    return float(found.group(1)) if found else None


def _age_at_arrival(anchor_age: int | None, anchor_year: int | None,
                    intime: datetime | None) -> float | None:
    """`anchor_age + (intime.year − anchor_year)`.

    ⚠ `services.demo_time.age_at()` 은 화면 표시용이라 0~120 밖이면 anchor_age 로
      되돌리는 보정이 있다. 배치에는 그 보정이 없으므로 여기서는 쓰지 않는다.
    """
    if anchor_age is None or anchor_year is None or intime is None:
        return None
    return float(anchor_age + (intime.year - anchor_year))


def load_patient(db: Session, stay_id: int) -> dict[str, Any] | None:
    """edstays + triage + patients → 모델 입력 patient dict. 없으면 None.

    관찰창도 함께 돌려준다 — `obs_end`(이 stay 의 관측 종료)와
    `lab_from`/`lab_to`(이 환자의 lab 관찰창). 배치와 같은 정의다.
    """
    cohort = bundle()["cohort"]
    row = db.execute(_PATIENT_SQL, {
        "stay_id": stay_id,
        "outtime_offset_h": cohort["ed_outtime_offset_h"],
        "horizon_h": cohort["horizon_h"],
    }).mappings().first()
    if row is None:
        return None

    t0 = row["t0"]
    temperature_f = row["triage_temperature_f"]
    return {
        "stay_id": row["stay_id"],
        "subject_id": row["subject_id"],
        "hadm_id": row["hadm_id"],
        "age": _age_at_arrival(row["anchor_age"], row["anchor_year"], row["ed_intime"]),
        "ed_intime": row["ed_intime"],
        "ed_outtime": row["ed_outtime"],
        "obs_end": row["obs_end"],
        "lab_from": t0 - timedelta(hours=LAB_WINDOW_H) if t0 is not None else None,
        "lab_to": row["t1"],
        "chiefcomplaint": row["chiefcomplaint"] or "",
        # 🚨 ℉ → ℃. 배치와 같이 범위 검사도 반올림도 하지 않는다.
        "triage_temperature": _f_to_c(temperature_f) if temperature_f else None,
        "triage_heartrate": row["triage_heartrate"],
        "triage_resprate": row["triage_resprate"],
        "triage_o2sat": row["triage_o2sat"],
        "triage_sbp": row["triage_sbp"],
        "triage_dbp": row["triage_dbp"],
        "triage_pain": _pain_number(row["triage_pain_text"]),
        "triage_acuity": float(row["triage_acuity"]) if row["triage_acuity"] is not None else None,
    }


# ---------------------------------------------------------------------- vitals

# 🔑 관찰창 `ed_intime <= charttime <= obs_end` 은 배치
#    (`src/data/extract_vitals.py` ed_vitals/chart_vitals)와 같은 조건이다.
#    하한이 없으면 내원 직전에 차팅된 측정이 섞여 배치와 값이 어긋난다.
#    인과 컷오프 `charttime <= t` 는 그대로 유지한다 — 미래 관측을 쓰지 않기 위함이다.
_ED_VITALS_SQL = text("""
    SELECT charttime, temperature, heartrate, resprate, o2sat, sbp, dbp
    FROM mimic.ed_vitalsign
    WHERE stay_id = :stay_id
      AND charttime >= :ed_intime
      AND charttime <= least(:t, :obs_end)
    ORDER BY charttime
""")

_CHARTEVENTS_SQL = text("""
    SELECT charttime, itemid, valuenum
    FROM mimic.chartevents
    WHERE hadm_id = :hadm_id
      AND charttime >= :ed_intime
      AND charttime <= least(:t, :obs_end)
      AND valuenum IS NOT NULL
      AND itemid IN :itemids
    ORDER BY charttime
""").bindparams(bindparam("itemids", expanding=True))


def load_vitals(db: Session, stay_id: int, hadm_id: int | None, t: datetime, *,
                ed_intime: datetime, obs_end: datetime) -> list[tuple[datetime, str, float]]:
    """ed/vitalsign(stay_id) + icu/chartevents(hadm_id) 를 합쳐 시간순.

    조건은 양쪽 모두 `ed_intime <= charttime <= min(t, obs_end)`.
    반환은 (charttime, 변수명, 값). 커버리지는 ed/vitalsign 95.5% · icu/chartevents 4.9% 다.
    """
    window = {"ed_intime": ed_intime, "obs_end": obs_end, "t": t}
    rows: list[tuple[datetime, str, float]] = []

    for row in db.execute(_ED_VITALS_SQL, {"stay_id": stay_id, **window}).mappings():
        charttime = row["charttime"]
        for column, var in _ED_VITAL_COLUMNS.items():
            value = row[column]
            if value is not None:
                rows.append((charttime, var, float(value)))
        # 🚨 ed_vitalsign.temperature 는 ℉ 다.
        if row["temperature"] is not None:
            rows.append((charttime, "temperature", _f_to_c(float(row["temperature"]))))

    if hadm_id is not None:
        itemids = _chartevents_itemids()
        chart_rows = db.execute(
            _CHARTEVENTS_SQL,
            {"hadm_id": hadm_id, "itemids": list(itemids), **window},
        ).mappings()
        for row in chart_rows:
            var, needs_convert = itemids[row["itemid"]]
            value = float(row["valuenum"])
            # 🚨 itemid 223761 은 ℉, 223762 는 ℃ 다.
            rows.append((row["charttime"], var, _f_to_c(value) if needs_convert else value))

    rows.sort(key=lambda item: item[0])
    return rows


# ------------------------------------------------------------------------ labs

# 🚨 avail_time = coalesce(storetime, charttime) — 결과를 실제로 볼 수 있었던 시각이다.
#    배치 `src/data/extract_labs.py` 와 같다. storetime 이 charttime 보다 중앙 0.90h 늦으므로
#    charttime 을 쓰면 아직 나오지 않은 결과를 쓰게 된다. 둘을 뒤바꾸지 않는다.
#    storetime 이 결측일 때만 charttime 으로 폴백한다.
#
# 관찰창 `t0 - 24h <= avail_time <= t1` 도 배치와 같다(t0·t1 은 subject 단위).
# 인과 컷오프 `avail_time <= t` 는 유지한다.
_LABS_SQL = text("""
    SELECT coalesce(storetime, charttime) AS avail_time, itemid, valuenum
    FROM mimic.labevents
    WHERE subject_id = :subject_id
      AND valuenum IS NOT NULL
      AND itemid IN :itemids
      AND coalesce(storetime, charttime) >= :lab_from
      AND coalesce(storetime, charttime) <= least(:t, :lab_to)
    ORDER BY avail_time
""").bindparams(bindparam("itemids", expanding=True))


def load_labs(db: Session, subject_id: int, t: datetime, *,
              lab_from: datetime, lab_to: datetime) -> list[tuple[datetime, str, float]]:
    """labevents. 반환은 (avail_time, 변수명, 값).

    avail_time 은 `coalesce(storetime, charttime)` 이고, 창은 `[lab_from, min(t, lab_to)]` 다.
    """
    itemids = _lab_itemids()
    rows = db.execute(
        _LABS_SQL,
        {"subject_id": subject_id, "t": t, "itemids": list(itemids),
         "lab_from": lab_from, "lab_to": lab_to},
    ).mappings()
    return [(row["avail_time"], itemids[row["itemid"]], float(row["valuenum"])) for row in rows]


# ----------------------------------------------------------------- 묶음 · 스케줄러

def load_model_input(db: Session, stay_id: int, t: datetime) -> dict[str, Any] | None:
    """riskmodel `/predict` 요청 body 를 통째로 만든다. 코호트에 없으면 None."""
    patient = load_patient(db, stay_id)
    if patient is None:
        return None

    return {
        # hadm_id 는 chartevents 조인에만, lab_from/lab_to 는 lab 창 계산에만 쓴다.
        # 셋 다 feature 가 아니라 riskmodel 요청 body 에서 뺀다.
        "patient": {k: v for k, v in patient.items()
                    if k not in ("hadm_id", "lab_from", "lab_to")},
        "vitals": load_vitals(db, stay_id, patient["hadm_id"], t,
                              ed_intime=patient["ed_intime"], obs_end=patient["obs_end"]),
        "labs": load_labs(db, patient["subject_id"], t,
                          lab_from=patient["lab_from"], lab_to=patient["lab_to"]),
        "t_end": t,
    }


_ACTIVE_STAYS_SQL = text("""
    SELECT d.ed_stay_id AS stay_id,
           e.subject_id,
           e.hadm_id,
           e.intime,
           e.outtime,
           d.has_departed,
           d.demo_offset,
           -- 화면 시각(데모 축) → 원본 시각. 모델은 원본 축에서만 계산한다.
           (app.demo_now() - d.demo_offset) AS t_now,
           -- 다음에 만들어야 할 예측 시점. 이미 계산된 마지막 시점 + step,
           -- 아직 없으면 ED 도착 + start_offset. 둘 다 bundle.json 값이다.
           COALESCE(p.last_prediction_time,
                    e.intime + make_interval(hours => :start_offset_h - :step_h))
               + make_interval(hours => :step_h) AS next_prediction_at
    FROM app.v_demo_stay d
    JOIN mimic.edstays e ON e.stay_id = d.ed_stay_id
    -- 🔑 예측 대상은 app.cohort 다. mimic.edstays 에는 lab 관찰창 하한(t0) 계산용
    --    raw ED 이력까지 들어 있어, 조인하지 않으면 코호트 밖 환자에게 예측이 생긴다.
    JOIN app.cohort c ON c.ed_stay_id = d.ed_stay_id
    LEFT JOIN LATERAL (
        -- 이미 저장된 마지막 예측 시점. 스케줄러가 '다음 시점'을 계산하는 근거다.
        -- 별도 컬럼(next_prediction_at)을 두지 않는 이유: app.prediction 이 정본이고
        -- 두 곳에 두면 upsert 와 어긋날 수 있다.
        SELECT max(pr.prediction_time) AS last_prediction_time
        FROM app.prediction pr
        WHERE pr.ed_stay_id = d.ed_stay_id
    ) p ON TRUE
    WHERE d.is_active
    ORDER BY d.ed_stay_id
""")


def list_stays_for_prediction(db: Session) -> list[Any]:
    """재예측 대상 stay + 그 stay 의 '지금'(원본 축) + 다음 예측 시점.

    퇴실 환자도 포함한다 — 적용 범위가 ED 퇴실 +2h 까지이고, 그 상한은
    riskmodel 이 bundle.json 기준으로 잘라내기 때문이다.

    ⚠ `next_prediction_at` 은 **원본 시간축**이다. 실행 슬롯은 화면(데모 축) 기준이라
      비교할 때 `demo_offset` 을 더해야 한다 — offset 은 15분 배수가 아니다.
    """
    grid = bundle()["grid"]
    return list(
        db.execute(
            _ACTIVE_STAYS_SQL,
            {"start_offset_h": grid["start_offset_h"], "step_h": grid["step_h"]},
        ).mappings()
    )
