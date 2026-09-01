"""온라인(DB 경유) ↔ 배치 파이프라인 일치 검증 — eron-project DB 버전.

원본 검증(모델 저장소)은 `cache/*.parquet` 에서 원본 입력을 재구성했다. 여기서는
**실제 서빙 경로와 같게** `backend/app/repositories/ml_features.py` 가 PostgreSQL 에서
꺼낸 입력을 쓴다. 배치 feature 는 모델 저장소의 `cache/features.parquet` 를 그대로
참조한다 — 같은 artifact(sha256 일치)로 만든 정본이다.

⚠ 이 검증을 통과하지 못하면 온라인 서빙 결과를 신뢰할 수 없다. 정기 실행할 것.
   과거 두 번의 불일치(변환기 재fit · 서빙 불가 feature 40개)는 **둘 다 에러 없이**
   지나갔고 성능만 조용히 떨어졌다.

두 컨테이너에 의존성이 나뉘어 있어 2단계로 실행한다.

  1) DB 입력 덤프 — backend 컨테이너 (SQLAlchemy·psycopg 보유, polars 없음)

     docker cp services/riskmodel/tests/test_online_parity.py eron-backend:/tmp/parity.py
     docker exec -w /app -e PYTHONPATH=/app eron-backend \\
         python /tmp/parity.py --dump-db /tmp/db_inputs.json
     docker cp eron-backend:/tmp/db_inputs.json ./db_inputs.json

  2) 비교 — riskmodel 이미지 (polars·lightgbm 보유, DB 접근 없음)

     docker run --rm \\
       -v <모델저장소>/cache:/batch/cache:ro \\
       -v $PWD/artifacts:/app/artifacts:ro \\
       -v $PWD:/parity:ro \\
       --entrypoint python eron-project-riskmodel:latest \\
       /parity/services/riskmodel/tests/test_online_parity.py \\
       --compare /parity/db_inputs.json --batch-cache /batch/cache

합격 기준(원본 검증과 동일)
    feature 최대 절대오차 < 1e-6 · 결측 불일치 0 · **위험도 최대 오차 < 1e-9**
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 배치 그리드보다 뒤의 관측까지 넉넉히 담는다. 빌더가 시점마다 `ts <= t` 로 다시
# 거르므로 여분은 결과에 영향을 주지 않는다. 모자라면 조용히 어긋난다.
DUMP_MARGIN_H = 24


# --------------------------------------------------------------------- 1) 덤프

def dump_db_inputs(out_path: str) -> int:
    """backend 컨테이너에서 실행. ml_features 어댑터가 꺼낸 원본 입력을 JSON 으로."""
    from app.database import SessionLocal          # noqa: PLC0415
    from app.repositories import ml_features as mf  # noqa: PLC0415

    def iso(value):
        return value.isoformat() if isinstance(value, datetime) else value

    db = SessionLocal()
    try:
        payload = []
        for row in mf.list_stays_for_prediction(db):
            stay_id = row["stay_id"]
            patient = mf.load_patient(db, stay_id)
            if patient is None or patient["ed_intime"] is None:
                continue
            end = (patient["ed_outtime"] or row["t_now"]) + timedelta(hours=DUMP_MARGIN_H)
            payload.append(dict(
                patient={k: iso(v) for k, v in patient.items()},
                vitals=[[ts.isoformat(), var, val]
                        for ts, var, val in mf.load_vitals(
                            db, stay_id, patient["hadm_id"], end,
                            ed_intime=patient["ed_intime"], obs_end=patient["obs_end"])],
                labs=[[ts.isoformat(), var, val]
                      for ts, var, val in mf.load_labs(
                          db, patient["subject_id"], end,
                          lab_from=patient["lab_from"], lab_to=patient["lab_to"])],
            ))
    finally:
        db.close()

    Path(out_path).write_text(json.dumps(payload), encoding="utf-8")
    print(f"DB 입력 덤프: {len(payload)} stay → {out_path}")
    return 0


# --------------------------------------------------------------------- 2) 비교

def _to_patient(raw: dict) -> dict:
    """JSON → 빌더가 받는 patient dict. 시각만 datetime 으로 되돌린다."""
    out = dict(raw)
    for key in ("ed_intime", "ed_outtime"):
        if out.get(key):
            out[key] = datetime.fromisoformat(out[key])
    return out


def compare(db_inputs_path: str, batch_cache: str, n_patients: int, art_dir: str) -> bool:
    import numpy as np                                       # noqa: PLC0415
    import polars as pl                                      # noqa: PLC0415

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from riskmodel.online_features import OnlineFeatureBuilder  # noqa: PLC0415
    from riskmodel.predict_service_with_reason_v3 import RiskService   # noqa: PLC0415

    builder = OnlineFeatureBuilder(art_dir=art_dir)
    service = RiskService(art_dir=art_dir)

    # 🚨 첫 예측 시점은 ED 도착 + start_offset_h 다. 하드코딩하면 설정이 바뀔 때 조용히
    #    1시간씩 어긋난 시점끼리 비교하게 된다(원본 저장소에서 실제로 발생했다).
    bundle = json.loads((Path(art_dir) / "bundle.json").read_text(encoding="utf-8"))
    start_h = int(bundle["grid"]["start_offset_h"])
    step_h = int(bundle["grid"]["step_h"])
    print(f"첫 예측 시점 = ED 도착 +{start_h}h · 간격 {step_h}h (bundle.json 기준)")

    db_rows = {str(d["patient"]["stay_id"]): d
               for d in json.loads(Path(db_inputs_path).read_text(encoding="utf-8"))}
    batch = pl.scan_parquet(f"{batch_cache}/features.parquet")
    # 배치 stay_id 는 문자열로 저장돼 있다.
    have = (batch.filter(pl.col("stay_id").is_in(list(db_rows)))
                 .select("stay_id", "t_idx", "t", *builder.features)
                 .collect())
    print(f"DB 코호트 {len(db_rows)} stay 중 배치에 존재: {have['stay_id'].n_unique()}")

    diffs, risk_diffs = [], []
    for stay_id, group in have.group_by("stay_id", maintain_order=True):
        stay_id = stay_id[0] if isinstance(stay_id, tuple) else stay_id
        b = group.sort("t_idx")
        if b.height < 3:
            continue
        d = db_rows[str(stay_id)]
        patient = _to_patient(d["patient"])

        vitals = [(datetime.fromisoformat(ts), var, val) for ts, var, val in d["vitals"]]
        labs = [(datetime.fromisoformat(ts), var, val) for ts, var, val in d["labs"]]

        t0 = patient["ed_intime"] + timedelta(hours=start_h)
        # 배치 그리드의 시작 시각과 실제로 같은지 확인한다. 다르면 비교 자체가 무의미하다.
        batch_t0 = b["t"][0]
        if isinstance(batch_t0, datetime) and abs((batch_t0 - t0).total_seconds()) > 1:
            print(f"  ⚠ stay {stay_id}: 그리드 시작 불일치 배치={batch_t0} DB={t0} — 건너뜀")
            continue

        online = builder.build_series(patient, vitals, labs, t0, b.height, step_h=step_h)

        cols = builder.features
        A = b.select(cols).to_numpy().astype(np.float64)
        B = online.select(cols).to_numpy().astype(np.float64)
        both = ~np.isnan(A) & ~np.isnan(B)
        cell_diff = np.abs(A - B)[both]
        nan_mismatch = int((np.isnan(A) != np.isnan(B)).sum())

        scored_a, scored_b = service.score_rows(b), service.score_rows(online)
        risk_a = scored_a["risk"].to_numpy()
        risk_b = scored_b["risk"].to_numpy()
        # 운영점(threshold) 적용 후 경보 라벨까지 같은지 본다. 확률이 threshold 양쪽에
        # 걸치면 미세한 차이도 화면 등급을 뒤집기 때문이다.
        label_mismatch = int((scored_a["alarm"].to_numpy() != scored_b["alarm"].to_numpy()).sum())

        # 가장 크게 어긋난 feature 를 함께 남긴다 — 원인 추적에 이게 없으면 손이 묶인다.
        worst = ""
        if cell_diff.size:
            per_col = np.where(both, np.abs(A - B), 0.0).max(axis=0)
            worst = cols[int(per_col.argmax())]

        diffs.append((str(stay_id), b.height,
                      float(cell_diff.max()) if cell_diff.size else 0.0,
                      float(cell_diff.mean()) if cell_diff.size else 0.0,
                      nan_mismatch, A.size, worst, label_mismatch))
        risk_diffs.append(float(np.abs(risk_a - risk_b).max()))
        if len(diffs) >= n_patients:
            break

    if not diffs:
        print("⚠ 비교 가능한 stay 가 없다. 배치 캐시와 DB 코호트가 겹치는지 확인할 것.")
        return False

    print(f"\n{'stay_id':<12}{'시점':>5}{'최대오차':>12}{'평균오차':>12}"
          f"{'결측불일치':>10}{'셀':>8}{'위험도오차':>12}  최대오차 feature")
    print("-" * 100)
    for (sid, n, mx, mn, nm, size, worst, lm), rd in zip(diffs, risk_diffs):
        print(f"{sid:<12}{n:>5}{mx:>12.2e}{mn:>12.2e}{nm:>10}{size:>8}{rd:>12.2e}  {worst}")

    all_max = max(x[2] for x in diffs)
    all_nan = sum(x[4] for x in diffs)
    all_label = sum(x[7] for x in diffs)
    all_risk = max(risk_diffs)
    n_pass = sum(1 for (x, rd) in zip(diffs, risk_diffs)
                 if x[2] < 1e-6 and x[4] == 0 and x[7] == 0 and rd < 1e-9)
    print(f"\n비교 stay              : {len(diffs)}  ·  시점 {sum(x[1] for x in diffs)}")
    print(f"전체 최대 feature 오차 : {all_max:.2e}   (기준 < 1e-6)")
    print(f"전체 결측 불일치        : {all_nan}")
    print(f"전체 최대 위험도 오차   : {all_risk:.2e}   (기준 < 1e-9)")
    print(f"예측 라벨 불일치        : {all_label}  (threshold {service.threshold:.6f})")
    print(f"통과 stay              : {n_pass}/{len(diffs)}")

    ok = all_max < 1e-6 and all_nan == 0 and all_risk < 1e-9 and all_label == 0
    print(f"\n{'✅ 배치와 일치 — 온라인 서빙 가능' if ok else '⚠ 불일치 — 원인 확인 필요'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dump-db", metavar="OUT.json",
                    help="backend 컨테이너에서 DB 원본 입력을 덤프한다")
    ap.add_argument("--compare", metavar="IN.json",
                    help="덤프한 DB 입력을 배치 feature 와 비교한다")
    ap.add_argument("--batch-cache", default="/batch/cache",
                    help="모델 저장소의 cache/ 경로 (features.parquet 이 있는 곳)")
    ap.add_argument("--artifacts", default="/app/artifacts")
    ap.add_argument("-n", type=int, default=5, help="비교할 stay 수")
    args = ap.parse_args()

    if args.dump_db:
        return dump_db_inputs(args.dump_db)
    if args.compare:
        return 0 if compare(args.compare, args.batch_cache, args.n, args.artifacts) else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
