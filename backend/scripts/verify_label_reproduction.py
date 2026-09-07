#!/usr/bin/env python3
"""악화 라벨 재현 검증 — 학습 파이프라인 정답과 행 단위로 대조한다.

    docker compose exec -T backend python scripts/verify_label_reproduction.py --truth - < truth.csv

🔑 왜 필요한가
    악화 라벨의 정본은 **학습 저장소**에 있고(전달 패키지에서 의도적으로 빠졌다 —
    `_handoff/README_배치안내.md` §7), 이 저장소에는
    `app/services/model_monitoring_label.py` 에 옮겨 적은 사본만 있다.
    단위 테스트는 상수가 바뀌지 않았는지만 지킨다. 학습 쪽 정의가 개정되면
    이쪽 사본은 조용히 낡은 채로 남는다 — 그러면 성능 숫자가 통째로 틀린다.
    이 스크립트는 **실제 정답과 맞춰 보는** 유일한 장치다.

정답 CSV 만들기 (학습 저장소에서 1회):

    # 1) 코호트 stay 목록
    docker compose exec -T postgres psql -U eron -d eron -At \\
        -c "SELECT ed_stay_id FROM app.cohort ORDER BY 1" > cohort_ids.txt

    # 2) 학습 산출물에서 그 stay 들만 추린다 (전체는 350만 행이라 느리다)
    cd <학습 저장소>
    .venv/bin/python - <<'EOF' > truth.csv
    import polars as pl
    ids = [x.strip() for x in open("cohort_ids.txt") if x.strip()]
    g = (pl.read_parquet("cache/grid_labels_C.parquet")
           .select("stay_id", "t", "y_deterioration")
           .filter(pl.col("stay_id").is_in(ids)))
    print("stay_id,t,y")
    for r in g.iter_rows():
        print(f"{r[0]},{r[1].isoformat()},{int(r[2])}")
    EOF

정답에 없는 (stay_id, t) 는 비교 대상이 아니다. 학습 grid 는
`min(obs_end, 첫 악화)` 에서 끊기므로 truncate 구간과 학습 코호트 밖 stay 는
애초에 정답이 존재하지 않는다. 그 내역은 따로 세어 보여 준다.

⚠ 이 스크립트는 **읽기 전용**이다. 판정 결과를 app.model_outcome 에 쓰지 않으므로
  화면이 쓰는 캐시를 건드리지 않는다. 데모 시계도 움직이지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import Session  # noqa: E402

from app.database import engine  # noqa: E402
from app.services import model_monitoring as svc  # noqa: E402


def load_truth(source: str) -> dict[tuple[int, datetime], int]:
    handle = sys.stdin if source == "-" else open(source, newline="", encoding="utf-8")
    try:
        reader = csv.DictReader(handle)
        return {
            (int(row["stay_id"]), datetime.fromisoformat(row["t"])): int(row["y"])
            for row in reader
        }
    finally:
        if handle is not sys.stdin:
            handle.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--truth", required=True,
                        help="학습 정답 CSV (stay_id,t,y). '-' 면 표준입력")
    parser.add_argument("--show", type=int, default=10, help="불일치 표시 개수")
    parser.add_argument(
        "--respect-demo-clock", action="store_true",
        help="데모 시계상 관찰창이 끝난 예측만 대조한다(화면과 같은 범위). "
             "기본은 전체를 대조한다 — 라벨의 정오는 데모 시계와 무관하다.",
    )
    args = parser.parse_args()

    truth = load_truth(args.truth)
    if not truth:
        print("[FATAL] 정답 CSV 가 비어 있습니다.", file=sys.stderr)
        return 2
    print(f"정답 {len(truth):,} 행 로드")

    with Session(engine) as db:
        # 검증은 캐시에 쓰지 않는다. 화면이 쓰는 판정 결과를 건드리면 안 된다.
        state = svc.evaluate(db, apply_demo_gate=args.respect_demo_clock, persist=False)

    compared = 0
    mismatches: list[tuple[int, datetime, int, int]] = []
    truth_positive = 0
    ours_positive = 0
    not_in_truth = 0
    not_in_truth_truncated = 0
    # 정답이 존재하는 예측이 몇 건인가 — 데모 시계가 이른 시점이면 대부분 아직 도래하지 않아
    # 대조 대상이 확 줄어든다. 그걸 "전체를 검증했다" 로 읽으면 안 된다.
    comparable = sum(1 for row in state.rows
                     if (row.ed_stay_id, row.prediction_time) in truth)

    for row in state.rows:
        # 판정하지 않은 행(미도래·중도절단)은 비교 대상이 아니다.
        if row.outcome_label is None:
            continue
        key = (row.ed_stay_id, row.prediction_time)
        expected = truth.get(key)
        if expected is None:
            not_in_truth += 1
            if row.is_truncated:
                not_in_truth_truncated += 1
            continue
        compared += 1
        truth_positive += expected
        ours_positive += row.outcome_label
        if expected != row.outcome_label:
            mismatches.append((row.ed_stay_id, row.prediction_time, expected, row.outcome_label))

    if not compared:
        print("[FATAL] 정답과 겹치는 예측이 없습니다. 코호트가 서로 다른 것 같습니다.",
              file=sys.stderr)
        return 2

    agreed = compared - len(mismatches)
    print(f"\n라벨 정의 : {state.label_definition}")
    print(f"대조 대상 : {compared:,} 행  (정답이 있는 예측 {comparable:,} 행 중)")
    print(f"일치      : {agreed:,} / {compared:,}  ({100 * agreed / compared:.2f}%)")
    print(f"양성      : 정답 {truth_positive} · 재현 {ours_positive}")
    print(f"정답 없음 : {not_in_truth} 행 (그중 truncate 구간 {not_in_truth_truncated})")

    if mismatches:
        print(f"\n❌ 불일치 {len(mismatches)} 행 (상위 {min(args.show, len(mismatches))}건)")
        for stay_id, moment, expected, got in mismatches[: args.show]:
            print(f"   stay {stay_id} · {moment}  정답 {expected} → 재현 {got}")
        print("\n학습 저장소의 라벨 정의가 바뀌었을 수 있습니다.")
        print("app/services/model_monitoring_label.py 를 학습 설정과 대조하세요.")
        return 1

    print("\n✅ 라벨 재현 일치 — 학습 정의와 같은 결과를 냅니다.")
    if comparable and compared < comparable * 0.5:
        print(f"\n⚠ 부분 검증이다. 정답이 있는 {comparable:,} 행 중 {compared:,} 행만 봤다.")
        print("  --respect-demo-clock 을 뺀 기본 모드로 실행하면 전체를 대조한다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
