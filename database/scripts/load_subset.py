#!/usr/bin/env python3
"""ER:ON — MIMIC-IV 행 서브셋 적재 (docs/database-design.md §7, §12)

    python3 database/scripts/load_subset.py                # 기본 배치
    python3 database/scripts/load_subset.py --demo-start   # 시연용 배치

app.cohort 에 고정된 ED stay 만 적재한다. 전체 적재가 아니다.
CSV 는 스트리밍으로 읽고, PostgreSQL COPY 로 밀어넣는다.
행 단위 INSERT 는 쓰지 않는다.

DB 접속은 docker compose 의 postgres 컨테이너 안에서 psql 을 실행해 처리한다.
(로컬에 psycopg / psql 을 설치할 필요가 없다)

    python3 database/scripts/load_subset.py
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from _db import copy_rows, log, psql, psql_file, rows as db_rows, scalar

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
ROOT = Path(os.environ.get("MIMIC_DATA_DIR", REPO))
INIT = REPO / "database" / "init"

# 재실 중으로 보여줄 목표 비율(적격 stay 기준). 나머지는 퇴실 상태가 된다.
IN_ED_RATIO = 75
# 퇴실 환자의 "퇴실 후 경과" 범위(분). 5분 ~ 8시간 사이에 흩뿌린다.
DEPARTED_LAG_MIN, DEPARTED_LAG_MAX = 5, 480
# '현재' 를 stay 안에 놓으려면 마지막 측정과 퇴실 사이에 최소 이만큼 여유가 필요하다.
MIN_IN_ED_WINDOW_MIN = 5

# --demo-start : 모든 환자를 '내원 직후' 상태로 놓는다.
# 데모 시계를 1시간씩 진행하면 vital 이 쌓이고 예측이 갱신되는 흐름을 보여줄 수 있다.
# 기본 배치는 now_ref 가 체류 구간 끝자락(평균 111%)에 있어 시계를 돌려도
# 퇴실만 일어나고 새 관측이 나타나지 않는다.
DEMO_START = "--demo-start" in sys.argv
DEMO_START_JITTER_MIN = 30
SEED = os.environ.get("COHORT_SEED", "20260826")

# --events-only : labevents / chartevents 만 다시 적재한다.
# 전체 적재는 app.demo_stay 를 새로 뽑아 시연 시나리오가 바뀌므로, 검사·ICU 활력징후만
# 고칠 때는 이 모드를 쓴다.
EVENTS_ONLY = "--events-only" in sys.argv

# --raw-history : mimic.edstays / mimic.admissions 를 코호트 환자의 전체 ED 이력으로
# 보완한다. 없는 행만 추가하므로 기존 시연 상태(app.demo_stay·bed_assignment)를 건드리지 않는다.
RAW_HISTORY = "--raw-history" in sys.argv

# chartevents 에 whitelist 를 걸되, 목록은 모델 번들에서 읽는다.
# 코드에 itemid 를 적어두면 모델이 개정될 때 조용히 어긋난다(실제로 224690 이 빠져 있었다).
BUNDLE = Path(os.environ.get("ARTIFACTS_DIR", REPO / "artifacts")) / "bundle.json"

# 모델이 쓰지 않지만 기존 적재에 포함돼 있던 항목. 데이터를 줄이는 것은 별도 결정이라
# 그대로 유지한다. (223835 = FiO2)
CHARTEVENTS_EXTRA_ITEMS = {"223835"}


def chartevents_itemids() -> set[str]:
    """artifacts/bundle.json["vital_itemids"] + 기존 유지 항목."""
    if not BUNDLE.exists():
        log(f"[FATAL] 모델 번들을 찾을 수 없습니다: {BUNDLE}")
        log("        ARTIFACTS_DIR 를 지정하거나 artifacts/ 를 저장소 루트에 두세요.")
        raise SystemExit(2)
    import json
    bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))
    wanted = {str(i) for ids in bundle["vital_itemids"].values() for i in ids}
    return wanted | CHARTEVENTS_EXTRA_ITEMS


# --------------------------------------------------------------------- utils

def require(path: Path) -> Path:
    if not path.exists():
        log(f"[FATAL] 파일을 찾을 수 없습니다: {path}")
        raise SystemExit(2)
    return path


def stream(path: Path):
    """gzip CSV 를 스트리밍 파싱. 헤더를 반환한 뒤 행을 흘린다."""
    with gzip.open(path, "rt", newline="") as f:
        r = csv.reader(f)
        header = next(r)
        yield header
        yield from r


# 가명용 성씨 풀. 실제 인구 분포를 모사하지 않는다 — 표기용 더미다.
SURNAMES = (
    "김 이 박 최 정 강 조 윤 장 임 한 오 서 신 권 황 안 송 류 전 "
    "홍 고 문 손 양 배 백 허 유 남"
).split()


def surname_for(stay_id: str) -> str:
    """stay_id 로부터 결정론적으로 성씨를 고른다. 같은 코호트면 항상 같은 결과."""
    h = int(hashlib.md5(f"{SEED}:name:{stay_id}".encode()).hexdigest(), 16)
    return SURNAMES[h % len(SURNAMES)]


def as_int(v: str) -> str:
    """'2.0000' 같은 float 문자열을 정수 컬럼용으로 정규화."""
    if not v:
        return ""
    try:
        return str(int(float(v)))
    except ValueError:
        return ""


# --------------------------------------------------------------------- main

def load_clinical_events(hosp: Path, icu: Path, subjects: set, hadms: set) -> dict[str, int]:
    """labevents · chartevents 적재.

    🔑 labevents 에는 시간창을 걸지 않는다. 코호트 환자의 검사 이력 전체를 담는다.
       모델의 lab_*_dt(마지막 검사 이후 경과시간)·lab_*_last 는 수개월~수년 전 검사까지
       참조하도록 학습됐다. 체류 구간 근처만 담으면 참조 대상이 더 과거로 밀려
       배치와 값이 어긋난다 — 에러 없이 성능만 떨어지므로 발견하기 어렵다.
       관측 시점 컷오프(storetime <= t)는 feature layer 가 적용한다.

    chartevents 는 itemid whitelist 를 걸되 목록을 모델 번들에서 읽는다.
    """
    counts: dict[str, int] = {}

    # labevents: labevent_id,subject_id,hadm_id,specimen_id,itemid,order_provider_id,
    #            charttime,storetime,value,valuenum,valueuom,...
    def rows_labevents():
        it = stream(require(hosp / "labevents.csv.gz")); next(it)
        for r_ in it:
            if r_[1] in subjects and r_[6]:
                yield [r_[0], r_[1], r_[2], r_[4], r_[6], r_[7], r_[9]]

    counts["mimic.labevents"] = copy_rows(
        "mimic.labevents",
        ["labevent_id", "subject_id", "hadm_id", "itemid",
         "charttime", "storetime", "valuenum"],
        rows_labevents())
    log(f"  labevents       {counts['mimic.labevents']:>6}  (시간창 없음 · 전체 이력)")

    # chartevents: subject_id,hadm_id,stay_id,caregiver_id,charttime,storetime,itemid,
    #              value,valuenum,valueuom,warning
    items = chartevents_itemids()
    log(f"  chartevents itemid whitelist {len(items)}종 (bundle.json 기준)")

    def rows_chartevents():
        it = stream(require(icu / "chartevents.csv.gz")); next(it)
        for r_ in it:
            if r_[1] in hadms and r_[6] in items and r_[4]:
                yield [r_[2], r_[0], r_[1], r_[6], r_[4], r_[8]]

    counts["mimic.chartevents"] = copy_rows(
        "mimic.chartevents",
        ["icu_stay_id", "subject_id", "hadm_id", "itemid", "charttime", "valuenum"],
        rows_chartevents())
    log(f"  chartevents     {counts['mimic.chartevents']:>6}")
    return counts


def raw_history() -> int:
    """mimic.edstays · mimic.admissions 를 코호트 환자의 전체 이력으로 보완한다(추가만).

    lab 관찰창의 하한 t0 = "그 환자의 최초 ED 내원 - 24h" 를 재현하려면 예측 대상 밖의
    과거 내원까지 raw source 에 있어야 한다. 예측 대상(app.cohort)은 그대로 둔다.
    """
    ed = require(ROOT / "MIMIC-IV-ED")
    hosp = require(ROOT / "MIMIC-IV-HOSP")

    log("[1/4] 스키마 적용 …")
    psql_file(require(INIT / "01_schema.sql"))

    cohort = db_rows("SELECT ed_stay_id, subject_id FROM app.cohort ORDER BY ed_stay_id")
    if not cohort:
        log("[FATAL] app.cohort 가 비어 있습니다.")
        return 2
    subjects = {r[1] for r in cohort}
    have_stays = {r[0] for r in db_rows("SELECT stay_id FROM mimic.edstays")}
    have_hadms = {r[0] for r in db_rows("SELECT hadm_id FROM mimic.admissions")}
    log(f"코호트 subject {len(subjects)} · 현재 edstays {len(have_stays)} · admissions {len(have_hadms)}")

    # admissions 를 먼저 넣어야 fk_edstays_hadm 이 깨지지 않는다.
    log("[2/4] admissions 보완 …")

    def rows_admissions():
        it = stream(require(hosp / "admissions.csv.gz")); next(it)
        for r_ in it:
            if r_[0] in subjects and r_[1] not in have_hadms:
                have_hadms.add(r_[1])
                yield [r_[1], r_[0], r_[2], r_[3], r_[4], r_[5], r_[7], r_[8],
                       r_[9], r_[11], r_[12], r_[13], r_[14], as_int(r_[15])]

    n_adm = copy_rows(
        "mimic.admissions",
        ["hadm_id", "subject_id", "admittime", "dischtime", "deathtime", "admission_type",
         "admission_location", "discharge_location", "insurance", "marital_status",
         "race", "edregtime", "edouttime", "hospital_expire_flag"],
        rows_admissions())
    log(f"  admissions  +{n_adm}")

    log("[3/4] edstays 보완 …")

    def rows_edstays():
        it = stream(require(ed / "edstays.csv.gz")); next(it)
        for r_ in it:
            if r_[0] in subjects and r_[2] not in have_stays:
                have_stays.add(r_[2])
                yield [r_[2], r_[0], r_[1], r_[3], r_[4], r_[5], r_[6], r_[7], r_[8]]

    n_ed = copy_rows(
        "mimic.edstays",
        ["stay_id", "subject_id", "hadm_id", "intime", "outtime",
         "gender", "race", "arrival_transport", "disposition"],
        rows_edstays())
    log(f"  edstays     +{n_ed}")

    log("[4/4] 인덱스 · 제약 · 검증 …")
    psql_file(require(INIT / "02_indexes.sql"))
    psql_file(require(INIT / "03_constraints.sql"))

    ok = True

    def check(label: str, sql: str, expect: str) -> None:
        nonlocal ok
        got = scalar(sql)
        good = got == expect
        ok = ok and good
        log(f"  {'✅' if good else '❌'} {label:<40} = {got:<8} (expect {expect})")

    check("코호트 → edstays 미적재", """count(*) FROM app.cohort c
            WHERE NOT EXISTS (SELECT 1 FROM mimic.edstays e WHERE e.stay_id = c.ed_stay_id)""", "0")
    check("edstays 가 코호트 환자만 담는가", """count(*) FROM mimic.edstays e
            WHERE NOT EXISTS (SELECT 1 FROM app.cohort c WHERE c.subject_id = e.subject_id)""", "0")
    check("edstays→admissions 고아", """count(*) FROM mimic.edstays e
            WHERE e.hadm_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM mimic.admissions a WHERE a.hadm_id = e.hadm_id)""", "0")
    check("예측 대상(app.cohort) 불변", "count(*) FROM app.cohort",
          str(len(cohort)))
    check("데모 시간축 불변", "count(*) FROM app.demo_stay", str(len(cohort)))

    log("")
    log(f"  mimic.edstays = {scalar('count(*) FROM mimic.edstays')} (raw 전체 ED 이력)")
    log(f"  app.cohort    = {scalar('count(*) FROM app.cohort')} (예측 대상)")
    log("")
    if not ok:
        log("❌ 검증 실패")
        return 1
    log("✅ raw ED 이력 보완 완료")
    return 0


def events_only() -> int:
    """labevents · chartevents 만 다시 적재한다. 시연 상태(app.*)는 건드리지 않는다."""
    hosp = require(ROOT / "MIMIC-IV-HOSP")
    icu = require(ROOT / "MIMIC-IV-ICU")

    log("[1/4] 스키마 적용 …")
    psql_file(require(INIT / "01_schema.sql"))

    cohort = db_rows("SELECT ed_stay_id, subject_id, hadm_id FROM app.cohort ORDER BY ed_stay_id")
    if not cohort:
        log("[FATAL] app.cohort 가 비어 있습니다.")
        return 2
    subjects = {r[1] for r in cohort}
    hadms = {r[2] for r in cohort if r[2]}
    log(f"코호트: subject {len(subjects)} · hadm {len(hadms)}")

    before_lab = scalar("count(*) FROM mimic.labevents")
    before_chart = scalar("count(*) FROM mimic.chartevents")

    log("[2/4] 대상 테이블만 비우기 …")
    psql("TRUNCATE mimic.labevents, mimic.chartevents RESTART IDENTITY;")

    log("[3/4] 재적재 …")
    counts = load_clinical_events(hosp, icu, subjects, hadms)

    log("[4/4] 인덱스 · 제약 · 검증 …")
    psql_file(require(INIT / "02_indexes.sql"))
    psql_file(require(INIT / "03_constraints.sql"))

    after_lab = scalar("count(*) FROM mimic.labevents")
    after_chart = scalar("count(*) FROM mimic.chartevents")
    log("")
    log(f"  labevents   {before_lab:>8} → {after_lab:>8}")
    log(f"  chartevents {before_chart:>8} → {after_chart:>8}")

    ok = True

    def check(label: str, sql: str, expect: str) -> None:
        nonlocal ok
        got = scalar(sql)
        good = got == expect
        ok = ok and good
        log(f"  {'✅' if good else '❌'} {label:<38} = {got:<8} (expect {expect})")

    check("lab 고아행(subject 미존재)", """count(*) FROM mimic.labevents l
            WHERE NOT EXISTS (SELECT 1 FROM mimic.patients p WHERE p.subject_id = l.subject_id)""", "0")
    check("chart 고아행(ICU stay 미존재)", """count(*) FROM mimic.chartevents c
            WHERE NOT EXISTS (SELECT 1 FROM mimic.icustays i WHERE i.icu_stay_id = c.icu_stay_id)""", "0")
    check("lab charttime 결측", "count(*) FROM mimic.labevents WHERE charttime IS NULL", "0")
    check("storetime 역전(보고 < 채혈)",
          "count(*) FROM mimic.labevents WHERE storetime < charttime", "0")
    check("모델 vital itemid 미적재",
          f"""count(*) FROM (VALUES {','.join(f"('{i}')" for i in sorted(chartevents_itemids() - CHARTEVENTS_EXTRA_ITEMS))}) v(i)
              WHERE NOT EXISTS (SELECT 1 FROM mimic.chartevents c WHERE c.itemid = v.i::int)""", "0")

    log("")
    if not ok:
        log("❌ 검증 실패 — 적재를 신뢰할 수 없습니다.")
        return 1
    log(f"✅ 재적재 완료 · {sum(counts.values()):,} 행")
    return 0


def main() -> int:
    if RAW_HISTORY:
        return raw_history()
    if EVENTS_ONLY:
        return events_only()
    ed = require(ROOT / "MIMIC-IV-ED")
    hosp = require(ROOT / "MIMIC-IV-HOSP")
    icu = require(ROOT / "MIMIC-IV-ICU")

    # 스키마부터 적용해야 app.cohort 를 읽을 수 있다
    log("[1/6] 스키마 적용 …")
    psql_file(require(INIT / "01_schema.sql"))

    # 코호트는 DB(app.cohort)가 원천이다. 파일에 두지 않는다.
    cohort = db_rows("SELECT ed_stay_id, subject_id, hadm_id FROM app.cohort ORDER BY ed_stay_id")
    if not cohort:
        log("[FATAL] app.cohort 가 비어 있습니다. 먼저 select_cohort.py 를 실행하세요.")
        return 2

    stays = {r[0] for r in cohort}
    subjects = {r[1] for r in cohort}
    hadms = {r[2] for r in cohort if r[2]}
    log(f"코호트(app.cohort): stay {len(stays)} · subject {len(subjects)} · hadm {len(hadms)}")

    # 2) 기존 서브셋 비우기 (재적재 가능하게). app.cohort 는 보존한다.
    log("[2/6] 기존 데이터 정리 …")
    psql("""
        -- 의존 순서: v_latest_* → v_demo_stay → v_ed_vitalsign_clean
        DROP VIEW IF EXISTS app.v_latest_prediction CASCADE;
        DROP VIEW IF EXISTS app.v_latest_vitalsign CASCADE;
        DROP VIEW IF EXISTS app.v_demo_stay CASCADE;
        DROP VIEW IF EXISTS mimic.v_ed_vitalsign_clean CASCADE;
        TRUNCATE app.alert, app.bed_assignment, app.bed, app.demo_stay,
                 app.patient_alias, app.prediction,
                 mimic.labevents, mimic.chartevents,
                 mimic.ed_vitalsign, mimic.ed_diagnosis, mimic.triage,
                 mimic.icustays, mimic.edstays, mimic.admissions, mimic.patients
        RESTART IDENTITY CASCADE;
    """)

    counts: dict[str, int] = {}
    intimes: dict[str, str] = {}
    outtimes: dict[str, str] = {}
    first_chart: dict[str, str] = {}
    last_chart: dict[str, str] = {}

    # 3) mimic 적재
    log("[3/6] MIMIC 서브셋 COPY …")

    # patients: subject_id,gender,anchor_age,anchor_year,anchor_year_group,dod
    def rows_patients():
        it = stream(require(hosp / "patients.csv.gz")); next(it)
        for r_ in it:
            if r_[0] in subjects:
                yield [r_[0], r_[1], as_int(r_[2]), as_int(r_[3]), r_[4], r_[5]]

    counts["mimic.patients"] = copy_rows(
        "mimic.patients",
        ["subject_id", "gender", "anchor_age", "anchor_year", "anchor_year_group", "dod"],
        rows_patients())
    log(f"  patients        {counts['mimic.patients']:>6}")

    # admissions
    # 🔑 코호트 hadm 이 아니라 코호트 subject 전체를 담는다.
    #    mimic.edstays 가 그 환자의 전체 ED 이력을 보존하므로(아래), 그 stay 들이 참조하는
    #    hadm 이 없으면 fk_edstays_hadm 이 깨진다. obs_end 계산에도 dischtime/deathtime 이 필요하다.
    def rows_admissions():
        it = stream(require(hosp / "admissions.csv.gz")); next(it)
        for r_ in it:
            if r_[0] in subjects:
                yield [r_[1], r_[0], r_[2], r_[3], r_[4], r_[5], r_[7], r_[8],
                       r_[9], r_[11], r_[12], r_[13], r_[14], as_int(r_[15])]

    counts["mimic.admissions"] = copy_rows(
        "mimic.admissions",
        ["hadm_id", "subject_id", "admittime", "dischtime", "deathtime", "admission_type",
         "admission_location", "discharge_location", "insurance", "marital_status",
         "race", "edregtime", "edouttime", "hospital_expire_flag"],
        rows_admissions())
    log(f"  admissions      {counts['mimic.admissions']:>6}")

    # edstays: subject_id,hadm_id,stay_id,intime,outtime,gender,race,arrival_transport,disposition
    #
    # 🔑 raw clinical source 와 prediction cohort 를 구분한다.
    #      mimic.edstays = 코호트 환자의 **전체 ED 내원 이력** (raw source)
    #      app.cohort    = ER:ON 데모의 **예측 대상** (그 부분집합)
    #    lab feature 의 관찰창 하한 t0 는 "그 환자의 최초 ED 내원 - 24h" 라서,
    #    코호트 stay 만 담으면 t0 가 뒤로 밀려 배치와 값이 어긋난다(실측 34명 중 17명만 일치).
    #    demo_stay·triage·vitalsign 은 예측 대상(app.cohort)에만 만든다.
    def rows_edstays():
        it = stream(require(ed / "edstays.csv.gz")); next(it)
        for r_ in it:
            if r_[0] in subjects:
                if r_[2] in stays:
                    # 데모 시간축 기준점은 예측 대상 stay 에서만 뽑는다
                    intimes[r_[2]] = r_[3]
                    outtimes[r_[2]] = r_[4]
                yield [r_[2], r_[0], r_[1], r_[3], r_[4], r_[5], r_[6], r_[7], r_[8]]

    counts["mimic.edstays"] = copy_rows(
        "mimic.edstays",
        ["stay_id", "subject_id", "hadm_id", "intime", "outtime",
         "gender", "race", "arrival_transport", "disposition"],
        rows_edstays())
    log(f"  edstays         {counts['mimic.edstays']:>6}")

    # triage
    def rows_triage():
        it = stream(require(ed / "triage.csv.gz")); next(it)
        for r_ in it:
            if r_[1] in stays:
                yield [r_[1], r_[0], r_[2], r_[3], r_[4], r_[5], r_[6], r_[7],
                       r_[8], as_int(r_[9]), r_[10]]

    counts["mimic.triage"] = copy_rows(
        "mimic.triage",
        ["stay_id", "subject_id", "temperature", "heartrate", "resprate", "o2sat",
         "sbp", "dbp", "pain", "acuity", "chiefcomplaint"],
        rows_triage())
    log(f"  triage          {counts['mimic.triage']:>6}")

    # vitalsign
    def rows_vitalsign():
        it = stream(require(ed / "vitalsign.csv.gz")); next(it)
        for r_ in it:
            if r_[1] in stays:
                # 데모 시간축 기준점으로 쓸 첫/마지막 측정 시각을 함께 모은다
                if r_[2] > last_chart.get(r_[1], ""):
                    last_chart[r_[1]] = r_[2]
                if r_[1] not in first_chart or r_[2] < first_chart[r_[1]]:
                    first_chart[r_[1]] = r_[2]
                yield [r_[1], r_[0], r_[2], r_[3], r_[4], r_[5], r_[6], r_[7],
                       r_[8], r_[9], r_[10]]

    counts["mimic.ed_vitalsign"] = copy_rows(
        "mimic.ed_vitalsign",
        ["stay_id", "subject_id", "charttime", "temperature", "heartrate", "resprate",
         "o2sat", "sbp", "dbp", "rhythm", "pain"],
        rows_vitalsign())
    log(f"  ed_vitalsign    {counts['mimic.ed_vitalsign']:>6}")

    # diagnosis
    def rows_diagnosis():
        it = stream(require(ed / "diagnosis.csv.gz")); next(it)
        for r_ in it:
            if r_[1] in stays:
                yield [r_[1], as_int(r_[2]), r_[0], r_[3], as_int(r_[4]), r_[5]]

    counts["mimic.ed_diagnosis"] = copy_rows(
        "mimic.ed_diagnosis",
        ["stay_id", "seq_num", "subject_id", "icd_code", "icd_version", "icd_title"],
        rows_diagnosis())
    log(f"  ed_diagnosis    {counts['mimic.ed_diagnosis']:>6}")

    # icustays
    def rows_icustays():
        it = stream(require(icu / "icustays.csv.gz")); next(it)
        for r_ in it:
            if r_[1] in hadms:
                yield [r_[2], r_[0], r_[1], r_[3], r_[4], r_[5], r_[6], r_[7]]

    counts["mimic.icustays"] = copy_rows(
        "mimic.icustays",
        ["icu_stay_id", "subject_id", "hadm_id", "first_careunit", "last_careunit",
         "intime", "outtime", "los"],
        rows_icustays())
    log(f"  icustays        {counts['mimic.icustays']:>6}")

    counts.update(load_clinical_events(hosp, icu, subjects, hadms))

    # 4) app 스캐폴딩 (D1 가명 · D6 데모 시간축 · D2 병상)
    log("[4/6] app 스키마 시드 …")
    ordered = sorted(stays, key=lambda s: int(s))

    # D1: 결정론적 가명. 성씨 + 마스킹 형태(김**)로 표기한다.
    #
    # MIMIC 은 완전 비식별화되어 이름이 없다. 성씨는 stay_id 해시로 결정론적으로
    # 배정한 가짜 값이며 실제 환자와 무관하다. 마스킹 형태를 쓰는 이유는
    # 완전한 이름을 지어내면 실존 인물로 오해될 여지가 있기 때문이다.
    # 동일 성씨가 여러 명 나오는 것은 정상이며, 식별은 환자번호(stay_id)로 한다.
    counts["app.patient_alias"] = copy_rows(
        "app.patient_alias", ["ed_stay_id", "display_name", "is_pseudonym"],
        ([s, f"{surname_for(s)}**", "true"] for s in ordered))

    # D6: 데모 시간축. 원천 timestamp 는 손대지 않고, 원본 시간축에서
    # '현재' 에 해당하는 시점(now_ref) 만 저장한다. 오프셋은 조회 시점에
    # now() - now_ref 로 계산되므로(app.v_demo_stay) 시간이 흘러도 어긋나지 않는다.
    #
    # 배치 규칙
    #   · 재실 중  : now_ref 를 (마지막 vital, 퇴실) 사이에 둔다.
    #                → 측정값은 전부 과거, 퇴실 시각은 미래 → "퇴실" 컬럼 빈칸
    #   · 퇴실 완료: now_ref = max(퇴실, 마지막 vital) + 5분~8시간
    #                → 모든 관측이 과거 → "퇴실" 컬럼에 유형 표시
    # 마지막 vital 이 퇴실보다 늦게 차팅된 stay(원본 데이터 특성)는 재실로 둘 수 없어
    # 자동으로 퇴실 처리된다.
    in_ed = departed_n = 0

    def rows_demo():
        nonlocal in_ed, departed_n
        for s_ in ordered:
            src_in = intimes.get(s_)
            src_out = outtimes.get(s_) or src_in
            src_last = last_chart.get(s_) or src_in
            if not src_in or not src_out or not src_last:
                continue

            t_out = datetime.fromisoformat(src_out)
            t_last = datetime.fromisoformat(src_last)
            h = int(hashlib.md5(f"{SEED}:demo:{s_}".encode()).hexdigest(), 16)

            if DEMO_START:
                # 첫 측정 직후에 '현재' 를 놓는다 → 최소 1건은 보이고 나머지는
                # 시계를 진행할수록 드러난다. 전원이 재실 상태로 시작한다.
                # 일부 stay 는 정식 접수(intime) 이전에 차팅된 vital 이 있다.
                # 그대로 쓰면 '미래에 내원한 환자' 가 생기므로 intime 이후로 클램프한다.
                t_first = max(
                    datetime.fromisoformat(first_chart.get(s_) or src_in),
                    datetime.fromisoformat(src_in),
                )
                now_ref = t_first + timedelta(minutes=h % DEMO_START_JITTER_MIN)
                in_ed += 1
                yield [s_, now_ref.isoformat(sep=" "), "true"]
                continue

            window_min = (t_out - t_last).total_seconds() / 60
            wants_in_ed = (h % 100) < IN_ED_RATIO

            if wants_in_ed and window_min > MIN_IN_ED_WINDOW_MIN:
                frac = 0.10 + (h // 100 % 81) / 100      # 창 안쪽 10~90% 지점
                now_ref = t_last + timedelta(minutes=window_min * frac)
                in_ed += 1
            else:
                span = DEPARTED_LAG_MAX - DEPARTED_LAG_MIN
                lag = DEPARTED_LAG_MIN + (h // 100 % span)
                now_ref = max(t_out, t_last) + timedelta(minutes=lag)
                departed_n += 1

            yield [s_, now_ref.isoformat(sep=" "), "true"]

    counts["app.demo_stay"] = copy_rows(
        "app.demo_stay", ["ed_stay_id", "now_ref", "is_active"], rows_demo())

    # D2: 병상 84개 (14구역 × 6). 화면 현황판은 이걸 48 + 36 두 페이지로 나눠 그린다.
    # 🔑 구역당 6병상을 유지해야 페이지 경계(A~H = 48, I~N = 36)가 구역을 쪼개지 않는다.
    zones = [(chr(ord("A") + i), f"{chr(ord('A') + i)} 구역") for i in range(14)]
    zones[0] = ("A", "A 구역 (Resus)")
    beds = [(f"{p}{i:02d}", label, zi * 10 + i)
            for zi, (p, label) in enumerate(zones) for i in range(1, 7)]
    counts["app.bed"] = copy_rows("app.bed", ["bed_id", "zone", "sort_order"], beds)

    # 데모 배정: 코호트 전원에게 병상을 준다. 목록에 보이는 환자가 현황판에도 보여야 한다.
    # (퇴실한 환자는 조회 시점에 v_demo_stay 가 걸러내므로 그 병상은 빈 병상으로 나온다)
    ranked = sorted(ordered, key=lambda s: hashlib.md5(f"{SEED}:bed:{s}".encode()).hexdigest())
    assigned = ranked[:len(beds)]
    if len(ranked) > len(beds):
        log(f"[WARN] 코호트 {len(ranked)}명 > 병상 {len(beds)}개 — "
            f"{len(ranked) - len(beds)}명은 현황판에 나오지 않는다")
    counts["app.bed_assignment"] = copy_rows(
        "app.bed_assignment", ["bed_id", "ed_stay_id", "devices"],
        ([beds[i][0], s, "{}"] for i, s in enumerate(assigned)))

    for k in ("app.patient_alias", "app.demo_stay", "app.bed", "app.bed_assignment"):
        log(f"  {k.split('.')[1]:<15} {counts[k]:>6}")
    log(f"    └ 재실 {in_ed}명 · 퇴실 {departed_n}명 (조회 시점 기준)"
        + ("  [--demo-start 배치]" if DEMO_START else ""))

    # 적재 직후에는 데모 시계를 실제 시각으로 초기화한다.
    # 이전 시연에서 앞당겨 둔 가상 시각이 새 데이터에 그대로 적용되면 안 된다.
    psql("""UPDATE app.demo_clock
               SET epoch_virtual = now(), anchor_real = now(),
                   anchor_virtual = now(), speed = 1, updated_at = now()
             WHERE id = 1""")

    # 5) 인덱스 · 제약 (적재 후)
    log("[5/6] 인덱스 · 제약 적용 …")
    psql_file(require(INIT / "02_indexes.sql"))
    psql_file(require(INIT / "03_constraints.sql"))
    psql_file(require(INIT / "04_views.sql"))

    # 6) 검증 (docs/database-design.md §7.6)
    log("[6/6] 검증 …")
    ok = True

    def check(label: str, sql: str, expect: str) -> None:
        nonlocal ok
        got = scalar(sql)
        good = got == expect
        ok = ok and good
        log(f"  {'✅' if good else '❌'} {label:<38} = {got:<8} (expect {expect})")

    # edstays 는 코호트보다 크다(전체 ED 이력). 개수가 아니라 포함관계를 본다.
    check("코호트 → edstays 미적재", """count(*) FROM app.cohort c
            WHERE NOT EXISTS (SELECT 1 FROM mimic.edstays e WHERE e.stay_id = c.ed_stay_id)""", "0")
    check("edstays 가 코호트 환자만 담는가", """count(*) FROM mimic.edstays e
            WHERE NOT EXISTS (SELECT 1 FROM app.cohort c WHERE c.subject_id = e.subject_id)""", "0")
    log(f"  ·  mimic.edstays = {scalar('count(*) FROM mimic.edstays')} (raw 전체 ED 이력)"
        f" · app.cohort = {scalar('count(*) FROM app.cohort')} (예측 대상)")
    check("triage 건수", "count(*) FROM mimic.triage", scalar("count(*) FROM app.cohort"))
    check("가명 건수", "count(*) FROM app.patient_alias", scalar("count(*) FROM app.cohort"))
    check("데모 시간축 건수", "count(*) FROM app.demo_stay", scalar("count(*) FROM app.cohort"))
    check("vital < 5회인 stay", """count(*) FROM (
            SELECT stay_id FROM mimic.ed_vitalsign GROUP BY stay_id HAVING count(*) < 5) t""", "0")
    check("vital 고아행", """count(*) FROM mimic.ed_vitalsign v
            WHERE NOT EXISTS (SELECT 1 FROM mimic.edstays e WHERE e.stay_id = v.stay_id)""", "0")
    check("edstays→patients 고아", """count(*) FROM mimic.edstays e
            WHERE NOT EXISTS (SELECT 1 FROM mimic.patients p WHERE p.subject_id = e.subject_id)""", "0")
    check("edstays→admissions 고아", """count(*) FROM mimic.edstays e
            WHERE e.hadm_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM mimic.admissions a WHERE a.hadm_id = e.hadm_id)""", "0")
    # 계층은 예측 대상(app.cohort) 기준이다. raw 이력까지 세면 안 된다.
    check("ICU 이동 stay(계층 A)", """count(DISTINCT e.stay_id) FROM mimic.edstays e
            JOIN app.cohort c ON c.ed_stay_id = e.stay_id
            JOIN mimic.icustays i ON i.hadm_id = e.hadm_id""",
          scalar("count(*) FROM app.cohort WHERE tier = 'A'"))
    check("acuity 결측", "count(*) FROM mimic.triage WHERE acuity IS NULL", "0")
    future_vitals = """count(*) FROM mimic.ed_vitalsign v
            JOIN app.v_demo_stay d ON d.ed_stay_id = v.stay_id
            WHERE v.charttime + d.demo_offset > app.demo_now()"""
    if DEMO_START:
        # 시연 배치에서는 아직 도래하지 않은 측정이 있는 것이 정상이다.
        # 데모 시계를 진행할 때 하나씩 드러나는 것이 이 모드의 목적이다.
        log(f"  ·  아직 도래하지 않은 vital = {scalar(future_vitals)} (--demo-start 에서는 정상)")
    else:
        check("미래 시각 vital", future_vitals, "0")
    check("미래 시각 내원",
          "count(*) FROM app.v_demo_stay WHERE demo_intime > app.demo_now()", "0")

    log("")
    log("  재실 / 퇴실 (조회 시점 기준):")
    log(psql("""SELECT has_departed AS departed, count(*)
                  FROM app.v_demo_stay GROUP BY 1 ORDER BY 1""", quiet=False))
    log("  acuity 분포:")
    log(psql("SELECT acuity, count(*) FROM mimic.triage GROUP BY acuity ORDER BY acuity", quiet=False))

    outliers = scalar("""count(*) FROM mimic.ed_vitalsign
        WHERE sbp > 300 OR heartrate > 300 OR o2sat > 100""")
    log(f"  (참고) 생리학적 범위 밖 vital 원본 행: {outliers} → 조회 시 view 에서 NULL 처리")

    log("")
    if not ok:
        log("❌ 검증 실패 — 적재를 신뢰할 수 없습니다.")
        return 1
    log(f"✅ 적재 완료 · 총 {sum(counts.values()):,} 행")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
