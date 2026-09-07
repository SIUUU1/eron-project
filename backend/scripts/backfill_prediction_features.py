#!/usr/bin/env python3
"""이미 저장된 예측에 model feature 값을 채워 넣는다 (drift 모니터링용).

    docker compose exec -T backend python scripts/backfill_prediction_features.py
    docker compose exec -T backend python scripts/backfill_prediction_features.py --dry-run

🔑 왜 필요한가
    feature 저장은 나중에 붙은 기능이라, 그 전에 만들어진 예측에는 feature 가 없다.
    그대로 두면 drift 는 앞으로 생길 예측만 보게 되어 한참을 기다려야 한다.
    이 스크립트는 riskmodel 을 다시 호출해 **같은 시점의 feature 를 받아** 채운다.

🔑 위험도를 다시 쓰지 않는다
    app.prediction 은 건드리지 않는다. 같은 입력이면 같은 확률이 나오지만,
    저장된 예측을 덮어쓸 이유가 없고 덮어쓰면 감사 흔적이 흐려진다.
    이 스크립트가 쓰는 곳은 app.prediction_feature 하나뿐이다.

⚠ riskmodel 이 떠 있어야 한다 (`docker compose --profile risk up -d riskmodel`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx2  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.database import engine  # noqa: E402
from app.repositories import ed_stays as repo  # noqa: E402
from app.repositories import ml_features as mf  # noqa: E402
from app.services.riskmodel import RiskModelClient  # noqa: E402

# 그 stay 의 마지막 예측 시점까지 그리드를 재생하면 저장된 모든 시점이 나온다.
_TARGETS = text("""
    SELECT p.ed_stay_id,
           max(p.prediction_time)                                   AS last_t,
           count(*)                                                 AS predictions,
           count(f.ed_stay_id)                                      AS with_features
      FROM app.prediction p
 LEFT JOIN app.prediction_feature f
        ON f.ed_stay_id = p.ed_stay_id
       AND f.prediction_time = p.prediction_time
       AND f.model_version = p.model_version
     GROUP BY p.ed_stay_id
     ORDER BY p.ed_stay_id
""")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="쓰지 않고 대상만 센다")
    parser.add_argument("--limit", type=int, default=0, help="처리할 stay 수 상한(0=전체)")
    parser.add_argument("--force", action="store_true",
                        help="이미 채워진 stay 도 다시 받아 덮어쓴다"
                             " (feature 구성이 바뀌었거나 값을 다시 받아야 할 때)")
    args = parser.parse_args()

    if not settings.predict_ai_url:
        print("[FATAL] PREDICT_AI_URL 이 없습니다. riskmodel 프로필을 켜세요.", file=sys.stderr)
        return 2

    with Session(engine) as db:
        targets = [row for row in db.execute(_TARGETS).mappings()]
        pending = targets if args.force else [
            row for row in targets if row["with_features"] < row["predictions"]
        ]
        if args.limit:
            pending = pending[: args.limit]

        total_predictions = sum(row["predictions"] for row in targets)
        covered = sum(row["with_features"] for row in targets)
        print(f"예측 {total_predictions:,} 건 / feature 보유 {covered:,} 건")
        print(f"채울 stay: {len(pending)} / {len(targets)}")
        if args.dry_run or not pending:
            return 0

        written = 0
        failed = 0
        async with httpx2.AsyncClient() as http_client:
            client = RiskModelClient(
                base_url=settings.predict_ai_url,
                timeout_seconds=settings.predict_ai_timeout_seconds,
                http_client=http_client,
            )
            for row in pending:
                stay_id = int(row["ed_stay_id"])
                payload = mf.load_model_input(db, stay_id, row["last_t"])
                if payload is None:
                    failed += 1
                    continue
                payload["include_features"] = True
                try:
                    result = await client.predict(payload)
                except Exception as exc:  # noqa: BLE001
                    print(f"  stay {stay_id} 실패: {exc}", file=sys.stderr)
                    failed += 1
                    continue

                names = result.get("feature_names")
                if not names:
                    print("[FATAL] riskmodel 이 feature 를 돌려주지 않습니다."
                          " include_features 를 지원하는 버전인지 확인하세요.", file=sys.stderr)
                    return 2

                rows = [
                    {
                        "ed_stay_id": stay_id,
                        "prediction_time": point["t"],
                        "model_version": result["model_version"],
                        "feature_hash": result.get("feature_hash"),
                        "feature_values": json.dumps(point["feature_values"]),
                    }
                    for point in result["predictions"]
                    if point.get("feature_values")
                ]
                written += repo.upsert_prediction_features(db, rows)

        print(f"\n기록 {written:,} 행 · 실패 {failed} stay")
        remaining = db.execute(text("""
            SELECT count(*) FROM app.prediction p
             WHERE NOT EXISTS (
                 SELECT 1 FROM app.prediction_feature f
                  WHERE f.ed_stay_id = p.ed_stay_id
                    AND f.prediction_time = p.prediction_time
                    AND f.model_version = p.model_version)
        """)).scalar_one()
        print(f"아직 feature 가 없는 예측: {remaining:,} 건")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
