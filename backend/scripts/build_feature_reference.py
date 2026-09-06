#!/usr/bin/env python3
"""학습 feature 분포에서 drift 참조 구간(bin)을 뽑는다.

    <학습 저장소>/.venv/bin/python backend/scripts/build_feature_reference.py \\
        --features <학습 저장소>/cache/features.parquet \\
        --bundle artifacts/bundle.json \\
        --out backend/app/data/feature_reference.json

🔑 왜 이 파일이 필요한가
    PSI 는 "학습 때 분포" 와 "지금 분포" 를 구간별로 비교한다. 그런데 이 저장소에 있는
    `artifacts/feature_spec.json` 에는 p01·p50·p99 세 분위뿐이라 구간을 만들 수 없다.
    그래서 학습 산출물에서 **구간 경계와 구간별 비율만** 한 번 뽑아 둔다.
    환자 데이터가 아니라 집계값이다(경계값과 비율).

🔑 왜 '10등분' 이라고 가정하지 않는가
    `heart_rate_n` 처럼 값이 뭉쳐 있는 feature 는 분위 경계가 겹쳐 구간이 줄어든다.
    경계를 중복 제거한 뒤 **실제로 세어** 비율을 기록한다. 0.1 로 가정하면 PSI 가 틀어진다.

🔑 결측은 별도 구간이다
    lab_*_dt 계열은 학습에서도 결측률이 80% 를 넘는다. 결측 비율 변화 자체가 drift 이므로
    빼지 않고 하나의 구간으로 센다.

⚠ 산출물에는 `feature_hash` 를 함께 적는다. feature 구성이 바뀌면 이 참조를 쓰면 안 된다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 학습 저장소의 venv 로 실행하므로 polars 가 있다. 이 저장소 backend 에는 없다.
try:
    import polars as pl
except ImportError:  # pragma: no cover - 실행 환경 안내
    print("[FATAL] polars 가 필요합니다. 학습 저장소의 .venv 로 실행하세요.", file=sys.stderr)
    raise SystemExit(2)

DEFAULT_BINS = 10


def build(frame: "pl.DataFrame", names: list[str], bins: int) -> dict:
    features: dict[str, dict] = {}
    skipped: list[str] = []

    for name in names:
        if name not in frame.columns:
            skipped.append(name)
            continue
        column = frame.get_column(name).cast(pl.Float64, strict=False)
        total = column.len()
        present = column.drop_nulls()
        missing_rate = (total - present.len()) / total if total else 0.0

        if present.is_empty():
            skipped.append(name)
            continue

        # 분위 경계. 겹치는 경계는 하나로 합친다(값이 뭉친 feature 대응).
        quantiles = [present.quantile(i / bins) for i in range(1, bins)]
        edges = sorted({q for q in quantiles if q is not None})

        # 실제로 세어 비율을 낸다 — 10등분이라고 가정하지 않는다.
        counts = [0] * (len(edges) + 1)
        for value in present.to_list():
            index = 0
            while index < len(edges) and value > edges[index]:
                index += 1
            counts[index] += 1

        present_total = present.len()
        features[name] = {
            "edges": edges,
            # 결측이 아닌 값들 안에서의 구간 비율. 합이 1 이다.
            "expected": [count / present_total for count in counts],
            "missing_rate": missing_rate,
        }

    return {"features": features, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", required=True, help="학습 feature parquet 경로")
    parser.add_argument("--bundle", required=True, help="artifacts/bundle.json 경로")
    parser.add_argument("--out", required=True, help="출력 JSON 경로")
    parser.add_argument("--bins", type=int, default=DEFAULT_BINS)
    args = parser.parse_args()

    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
    names = list(bundle["features"])

    frame = pl.read_parquet(args.features, columns=None)
    print(f"학습 feature 행 {frame.height:,} · 컬럼 {len(frame.columns):,}")

    result = build(frame, names, args.bins)
    if result["skipped"]:
        print(f"⚠ 건너뛴 feature {len(result['skipped'])}개: {result['skipped'][:5]} …")

    payload = {
        "source": Path(args.features).name,
        "model_version": bundle["version"],
        # 이 값이 다르면 feature 구성이 바뀐 것이다. 그 경우 이 참조를 쓰면 안 된다.
        "feature_hash": bundle["feature_hash"],
        "n_rows": frame.height,
        "bins": args.bins,
        "features": result["features"],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    size_kb = out.stat().st_size / 1024
    print(f"저장: {out}  ({len(result['features'])} feature · {size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
