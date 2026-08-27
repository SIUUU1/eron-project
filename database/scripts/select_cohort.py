#!/usr/bin/env python3
"""ER:ON — 개발용 ED stay 코호트 선별 (docs/database-design.md §7)

MIMIC-IV 전체를 적재하지 않는다. 프론트엔드가 실제로 필요로 하는 데이터와
악화 예측에 필요한 결과 라벨을 담을 수 있는 최소 코호트만 고른다.

출력: PostgreSQL 의 app.cohort 테이블
      ed_stay_id, subject_id, hadm_id, tier, acuity, vital_count, seed

코호트 정의를 파일이 아니라 DB 에 둔다. 파일에 두면 저장소에 커밋되지 않아
팀원 간 코호트가 어긋날 수 있고, DB 만 봐서는 어떤 기준으로 뽑힌 환자인지
알 수 없다. postgres 컨테이너가 떠 있어야 한다.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import os
from collections import Counter, defaultdict
from pathlib import Path

from _db import copy_rows, log, psql, psql_file

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
ROOT = Path(os.environ.get("MIMIC_DATA_DIR", REPO))
INIT = REPO / "database" / "init"

SEED = os.environ.get("COHORT_SEED", "20260826")
MIN_VITALS = 5

# (tier, acuity) -> 뽑을 건수.  docs/database-design.md §7.3 확정 쿼터
QUOTA: dict[tuple[str, int], int] = {
    ("A", 1): 32, ("A", 2): 50, ("A", 3): 16, ("A", 4): 2,  ("A", 5): 0,
    ("B", 1): 8,  ("B", 2): 36, ("B", 3): 24, ("B", 4): 2,  ("B", 5): 0,
    ("C", 1): 8,  ("C", 2): 48, ("C", 3): 48, ("C", 4): 4,  ("C", 5): 2,
    ("D", 1): 8,  ("D", 2): 8,  ("D", 3): 4,  ("D", 4): 0,  ("D", 5): 0,
}
TIER_LABEL = {
    "A": "ICU 이동 (악화 양성)",
    "B": "입원, ICU 미이동",
    "C": "귀가 (악화 음성)",
    "D": "ED 사망",
}


def rank_key(stay_id: str) -> str:
    """결정론적 정렬키. SEED 가 같으면 언제 돌려도 같은 코호트가 나온다."""
    return hashlib.md5(f"{SEED}:{stay_id}".encode()).hexdigest()


def require(path: Path) -> Path:
    if not path.exists():
        log(f"[FATAL] 파일을 찾을 수 없습니다: {path}")
        log("        MIMIC_DATA_DIR 환경변수로 데이터 위치를 지정하세요.")
        raise SystemExit(2)
    return path


def main() -> int:
    ed = require(ROOT / "MIMIC-IV-ED")
    icu = require(ROOT / "MIMIC-IV-ICU")

    # 1) stay 별 vital 측정 횟수
    log("[1/5] vitalsign 스캔 …")
    vital_count: Counter[str] = Counter()
    with gzip.open(require(ed / "vitalsign.csv.gz"), "rt", newline="") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            vital_count[row[1]] += 1

    # 2) stay 별 acuity (ESI 1~5). '2.0000' 형태이므로 float 경유 캐스팅.
    log("[2/5] triage 스캔 …")
    acuity: dict[str, int] = {}
    with gzip.open(require(ed / "triage.csv.gz"), "rt", newline="") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            raw = row[9]
            if not raw:
                continue
            try:
                a = int(float(raw))
            except ValueError:
                continue
            if 1 <= a <= 5:
                acuity[row[1]] = a

    # 3) ICU 로 이어진 입원 건 (hadm_id 는 2번째 컬럼)
    log("[3/5] icustays 스캔 …")
    icu_hadm: set[str] = set()
    with gzip.open(require(icu / "icustays.csv.gz"), "rt", newline="") as f:
        r = csv.reader(f)
        header = next(r)
        assert header[1] == "hadm_id", f"icustays 컬럼 순서가 예상과 다릅니다: {header}"
        for row in r:
            if row[1]:
                icu_hadm.add(row[1])

    # 4) 적격 후보를 (tier, acuity) 버킷으로 모은다
    log("[4/5] edstays 스캔 · 계층 분류 …")
    buckets: dict[tuple[str, int], list[tuple[str, str, str, str, int]]] = defaultdict(list)
    pool = Counter()
    with gzip.open(require(ed / "edstays.csv.gz"), "rt", newline="") as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            subject_id, hadm_id, stay_id = row[0], row[1], row[2]
            disposition = row[8]

            n = vital_count.get(stay_id, 0)
            if n < MIN_VITALS:
                continue
            a = acuity.get(stay_id)
            if a is None:
                continue

            if hadm_id and hadm_id in icu_hadm:
                tier = "A"
            elif disposition == "ADMITTED":
                tier = "B"
            elif disposition == "HOME":
                tier = "C"
            elif disposition == "EXPIRED":
                tier = "D"
            else:
                continue

            buckets[(tier, a)].append((rank_key(stay_id), stay_id, subject_id, hadm_id, n))
            pool[(tier, a)] += 1

    # 5) 쿼터만큼 뽑는다. 모자라면 같은 tier 의 다른 acuity 에서 보충.
    log("[5/5] 쿼터 적용 …")
    for key in buckets:
        buckets[key].sort()

    chosen: list[tuple[str, str, str, str, int, int]] = []  # stay, subject, hadm, tier, acuity, n
    taken: dict[tuple[str, int], int] = {}
    shortfall: dict[str, int] = Counter()

    for (tier, a), want in sorted(QUOTA.items()):
        have = buckets.get((tier, a), [])
        take = min(want, len(have))
        taken[(tier, a)] = take
        if take < want:
            shortfall[tier] += want - take
            log(f"  [warn] {tier}/acuity{a}: 요청 {want} · 가용 {len(have)} → {want - take}건 부족")
        for _, stay_id, subject_id, hadm_id, n in have[:take]:
            chosen.append((stay_id, subject_id, hadm_id, tier, a, n))

    # tier 내 fallback 보충
    used = {c[0] for c in chosen}
    for tier, missing in shortfall.items():
        if missing <= 0:
            continue
        spare: list[tuple[str, str, str, str, int, int]] = []
        for a in range(1, 6):
            for _, stay_id, subject_id, hadm_id, n in buckets.get((tier, a), []):
                if stay_id not in used:
                    spare.append((stay_id, subject_id, hadm_id, tier, a, n))
        spare.sort(key=lambda x: rank_key(x[0]))
        add = spare[:missing]
        if len(add) < missing:
            log(f"  [warn] tier {tier}: fallback 후에도 {missing - len(add)}건 부족")
        for row in add:
            used.add(row[0])
        chosen.extend(add)

    chosen.sort(key=lambda x: (x[3], -x[4], x[0]))

    # 스키마를 먼저 적용해야 app.cohort 가 존재한다
    psql_file(require(INIT / "01_schema.sql"))
    psql("TRUNCATE app.cohort")
    n = copy_rows(
        "app.cohort",
        ["ed_stay_id", "subject_id", "hadm_id", "tier", "acuity", "vital_count", "seed"],
        ([stay, subject, hadm, tier, acuity, vc, SEED]
         for stay, subject, hadm, tier, acuity, vc in chosen),
    )

    # 요약
    by_tier = Counter(c[3] for c in chosen)
    by_acuity = Counter(c[4] for c in chosen)
    log("")
    log(f"  코호트 {n}건 → app.cohort (seed={SEED})")
    log("  계층별:")
    for t in "ABCD":
        log(f"    {t} {TIER_LABEL[t]:<22} {by_tier[t]:4d}  (적격 풀 {sum(pool[(t, a)] for a in range(1, 6)):,})")
    log("  acuity 별: " + "  ".join(f"a{a}={by_acuity[a]}" for a in range(1, 6)))
    log(f"  vital 행 예상: {sum(c[5] for c in chosen):,}")

    missing_acuity = [a for a in range(1, 6) if by_acuity[a] == 0]
    if missing_acuity:
        log(f"  [warn] acuity {missing_acuity} 가 코호트에 없습니다 — 목록 화면 배지가 일부 렌더되지 않습니다")

    if len(chosen) != sum(QUOTA.values()):
        log(f"  [warn] 목표 {sum(QUOTA.values())}건 중 {len(chosen)}건만 선별되었습니다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
