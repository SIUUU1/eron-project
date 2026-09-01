"""악화 예측 실행 — DB 원본 → riskmodel → 등급 → app.prediction 기록.

    ml_features(원본 관측)  →  riskmodel /predict  →  보정 확률
                                                    →  services.risk 등급
                                                    →  app.prediction upsert

🔑 예측 대상은 **app.cohort(83건)** 다. `mimic.edstays` 는 lab 관찰창의 하한 t0 를
   계산하기 위한 raw ED 이력(180건)이라 예측 대상이 아니다. 둘을 섞으면 코호트 밖
   환자에게 예측이 생긴다.

🔑 시간축. `mimic.*` 와 `app.prediction` 은 MIMIC 원본 축이고, 화면은 거기에
   demo_offset 을 더한 데모 축이다. 모델에 넘기는 `t` 와 저장하는 prediction_time 은
   반드시 원본 축이어야 한다 — 변환은 ml_features.list_stays_for_prediction 이 한다.

⚠ 확률은 riskmodel 이 준 **보정 확률을 그대로** 저장한다. 여기서 다시 계산하거나
  반올림하지 않는다. 등급 경계만 .env(RISK_*) 에서 온다.

⚠ 설명(reason)도 모델이 만든 문장을 detail 에 **그대로** 옮긴다. backend 는 문구를
  만들지 않는다 — 만들면 모델이 실제로 본 신호와 화면 문구가 어긋난다.

━━ 실행 스케줄 (2026-09-01 변경) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

예측 시점(prediction_time)은 **환자 입실 시각 기준 1시간 간격** 그대로다. 바뀐 것은
"언제 계산하러 가는가" 뿐이다.

    환자별 next_prediction_at = 마지막 예측 시점 + 1h (없으면 ED 도착 + 1h)
            ↓  due?  next_prediction_at <= t_now   (원본 축)
            ↓  slot = ceil15(next_prediction_at + demo_offset)   (데모 축)
            ↓  slot <= 현재 슬롯(floor15(demo_now)) 인 환자만
            ↓  같은 슬롯 환자를 한 batch 로 묶어 동시성 제한을 걸고 호출

🔑 왜 올림(ceiling)인가
   riskmodel 은 t_end(=지금)까지만 그리드를 만든다. 11:07 예측을 11:00 슬롯에서
   실행하면 그 행은 아예 만들어지지 않는다(미리 계산되지 않으므로 leakage 도 없다).
   그래서 11:07 은 11:15 슬롯이 맞다.

🔑 왜 prediction_time 을 슬롯에 맞추지 않는가
   prediction_time 은 곧 모델 입력 시각 t 다. vital charttime <= t · lab storetime <= t ·
   hours_from_ed 가 전부 t 에 걸려 있고, 그리드 시작점/간격은 bundle.json 이 정본이다.
   여기를 흔들면 배치 파이프라인 일치 검증(test_online_parity)이 조용히 깨진다.

🔑 놓친 슬롯은 저절로 따라잡힌다
   선택 조건이 "slot <= 현재 슬롯" 이라 서버가 내려갔다 와도, 데모 배속으로 여러
   슬롯이 한꺼번에 지나가도 밀린 환자가 그대로 대상이 된다. 한 번의 호출이 그리드를
   처음부터 재생하므로 중간 시점까지 함께 채워진다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx2
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import SessionLocal
from app.repositories import ed_stays as repo
from app.repositories import ml_features as mf
from app.services import risk
from app.services.riskmodel import (
    InvalidRiskModelResponseError,
    RiskModelClient,
    RiskModelUnavailableError,
)

logger = logging.getLogger(__name__)

# 1시간을 네 개의 실행 슬롯(00/15/30/45)으로 나눈다.
SLOT_MINUTES = 15


def slot_for(moment: datetime) -> datetime:
    """예측 시각이 실행될 슬롯 — **올림**.

        11:00:00 → 11:00      11:15:00 → 11:15
        11:00:01 → 11:15      11:16:00 → 11:30
        11:07:00 → 11:15      11:46:00 → 12:00

    내림/반올림을 쓰면 예측 시각보다 이른 슬롯에 배정되어, 그 시점 행이 만들어지지
    않은 채 다음 슬롯까지 밀린다.
    """
    floor = moment.replace(
        minute=(moment.minute // SLOT_MINUTES) * SLOT_MINUTES,
        second=0,
        microsecond=0,
    )
    return floor if floor == moment else floor + timedelta(minutes=SLOT_MINUTES)


def current_slot(now: datetime) -> datetime:
    """지금까지 도래한 마지막 슬롯 — **내림**."""
    return now.replace(
        minute=(now.minute // SLOT_MINUTES) * SLOT_MINUTES,
        second=0,
        microsecond=0,
    )


def is_due(stay: Any) -> bool:
    """다음 예측 시점이 이미 지났는가(원본 시간축).

    미래 시점을 미리 계산하지 않기 위한 조건이다.
    """
    return stay["next_prediction_at"] <= stay["t_now"]


def _beyond_horizon(stay: Any, outtime_offset_h: float) -> bool:
    """감시 구간(ED 퇴실 + offset)을 넘겼는가.

    넘긴 환자는 매시간 due 가 되지만 riskmodel 이 out_of_scope 로 빈 응답만 준다.
    헛호출을 막으려고 여기서 먼저 거른다. 상한을 실제 obs_end 보다 **넉넉하게**
    잡으므로(퇴원·사망 시각은 여기서 모른다) 필요한 예측을 건너뛰지 않는다.
    """
    outtime = stay["outtime"]
    if outtime is None:
        return False
    return stay["next_prediction_at"] > outtime + timedelta(hours=outtime_offset_h)


def select_stays(
    stays: list[Any],
    *,
    force_all: bool = False,
    slot_limit: datetime | None = None,
    outtime_offset_h: float = 2.0,
) -> list[Any]:
    """이번 실행에서 계산할 환자만 고른다.

    force_all 이면 예전처럼 전원을 돌린다(데모 시계를 되돌린 뒤 복구용).
    """
    if force_all:
        return list(stays)

    picked = []
    for stay in stays:
        if not is_due(stay):
            continue
        if _beyond_horizon(stay, outtime_offset_h):
            continue
        if slot_limit is not None:
            # 슬롯은 화면(데모 축) 기준이다. demo_offset 은 15분 배수가 아니므로
            # 원본 축 시각을 그대로 쓰면 슬롯이 어긋난다.
            demo_moment = stay["next_prediction_at"] + stay["demo_offset"]
            if slot_for(demo_moment) > slot_limit:
                continue
        picked.append(stay)
    return picked


def _to_rows(stay_id: int, result: dict[str, Any]) -> list[dict[str, Any]]:
    """riskmodel 응답 → app.prediction 행. 적용 범위 밖이면 빈 목록."""
    import json

    if not result.get("in_scope"):
        return []

    model_version = result["model_version"]
    window_h = result.get("window_h")
    horizon_minutes = int(window_h * 60) if window_h is not None else None
    threshold = result.get("threshold")

    # 설명은 화면에서 반드시 이 문구와 함께 표시한다(인과관계 아님).
    reason_notice = result.get("reason_notice")
    # 어떤 임상 방향 gate 로 걸러진 설명인지 함께 남긴다.
    gate_version = result.get("clinical_gate_version")

    rows = []
    for point in result["predictions"]:
        probability = float(point["risk"])
        rows.append({
            "ed_stay_id": stay_id,
            "model_version": model_version,
            "prediction_time": point["t"],
            "t_idx": point.get("t_idx"),
            "horizon_minutes": horizon_minutes,
            "risk_probability": probability,
            # 4단계 등급. 경계는 .env 의 RISK_* → core.config → services.risk 로 온다.
            "risk_level": risk.level_from_probability(probability),
            # 모델이 준 3구간(green/amber/red)과 운영점을 함께 남긴다.
            # 화면이 등급 근거를 되짚을 수 있어야 하고, 경계를 바꿨을 때 대조도 된다.
            "detail": json.dumps({
                "band": point.get("band"),
                "alarm": point.get("alarm"),
                "threshold": threshold,
                "operating_point": result.get("operating_point"),
                # 설명(기여 신호). 모델이 준 문장을 그대로 저장하고 여기서 만들지 않는다.
                # reason_type: risk_increase_signal(직전 대비 상승) | current_risk_signal(현재 위험)
                # reason_detail 은 v3 부터 **model feature 1개** 단위다(그룹 합산 아님).
                # ⚠ reason_detail 이 비어 있어도 reason_type/title 은 의미가 있다.
                #   "위험도는 올랐지만 확인된 임상적 악화 신호가 없음" 상태다.
                "reason": point.get("reason"),
                "reason_type": point.get("reason_type"),
                "reason_title": point.get("reason_title"),
                "reason_basis": point.get("reason_basis"),
                "reason_detail": point.get("reason_detail") or [],
                # 모델이 임상 방향 gate 를 통과시킨 문장만 담은 목록(정본).
                "risk_factors": [x.get("text") for x in point.get("reason_detail") or []],
                "clinical_worsening_confirmed": point.get("clinical_worsening_confirmed"),
                "clinical_gate_version": gate_version,
                "reason_notice": reason_notice,
                "risk_delta": point.get("risk_delta"),
                "risk_delta_pct_point": point.get("risk_delta_pct_point"),
            }, ensure_ascii=False),
        })
    return rows


async def _predict_batch(
    client: RiskModelClient,
    payloads: list[tuple[int, dict[str, Any]]],
) -> list[tuple[int, dict[str, Any] | None]]:
    """한 슬롯의 환자들을 동시성 제한을 걸고 호출한다.

    riskmodel 은 환자 1명 단위 API 다(batch endpoint 없음). 무제한 gather 로 던지면
    uvicorn 워커 하나에 요청이 몰려 타임아웃이 난다. 세마포어로 묶어서 보낸다.
    실패는 예외를 올리지 않고 None 으로 돌려 호출부가 기존 정책대로 세게 한다.
    """
    limit = max(1, int(settings.predict_batch_concurrency))
    sem = asyncio.Semaphore(limit)

    async def one(stay_id: int, payload: dict[str, Any]):
        async with sem:
            try:
                return stay_id, await client.predict(payload)
            except (RiskModelUnavailableError, InvalidRiskModelResponseError):
                return stay_id, None

    return list(await asyncio.gather(*(one(sid, pl) for sid, pl in payloads)))


async def run_once(
    db: Session,
    client: RiskModelClient,
    *,
    force_all: bool = False,
    slot_limit: datetime | None = None,
) -> dict[str, Any]:
    """이번 슬롯에서 계산이 필요한 환자만 재예측한다. 반환은 실행 요약.

    force_all=True 면 예전처럼 코호트 전원을 다시 계산한다.
    슬롯·due 판정은 select_stays 한 곳에만 있고, 스케줄러와 수동 실행이 같이 쓴다.
    """
    stays = mf.list_stays_for_prediction(db)
    outtime_offset_h = mf.bundle()["cohort"]["ed_outtime_offset_h"]
    selected = select_stays(
        stays,
        force_all=force_all,
        slot_limit=slot_limit,
        outtime_offset_h=outtime_offset_h,
    )

    summary: dict[str, Any] = {
        "stays": len(stays),
        "selected": len(selected),
        "slot": slot_limit.isoformat() if slot_limit is not None else None,
        "scored": 0,
        "rows": 0,
        "out_of_scope": 0,
        "failed": 0,
    }
    if not selected:
        return summary

    # 1) DB 조회는 순차로. 같은 Session 을 여러 태스크가 동시에 쓰면 안 된다.
    payloads: list[tuple[int, dict[str, Any]]] = []
    for stay in selected:
        payload = mf.load_model_input(db, stay["stay_id"], stay["t_now"])
        if payload is not None:
            payloads.append((stay["stay_id"], payload))

    # 2) 모델 호출만 동시성 제한을 걸어 묶어서
    results = await _predict_batch(client, payloads)

    # 3) 기록도 순차로(같은 Session)
    for stay_id, result in results:
        if result is None:
            summary["failed"] += 1
            continue
        rows = _to_rows(stay_id, result)
        if not rows:
            summary["out_of_scope"] += 1
            continue
        summary["rows"] += repo.upsert_predictions(db, rows)
        summary["scored"] += 1

    return summary


def demo_now(db: Session) -> datetime:
    """화면(데모 축)의 현재 시각. 슬롯 계산 기준이다."""
    return db.execute(text("SELECT app.demo_now()")).scalar_one()


async def _tick(slot: datetime | None) -> dict[str, Any]:
    db = SessionLocal()
    try:
        async with httpx2.AsyncClient() as http_client:
            client = RiskModelClient(
                base_url=settings.predict_ai_url or "",
                timeout_seconds=settings.predict_ai_timeout_seconds,
                http_client=http_client,
            )
            summary = await run_once(db, client, slot_limit=slot)
    finally:
        db.close()

    if summary["failed"]:
        logger.warning("재예측: %s", summary)
    elif summary["selected"]:
        logger.info("재예측: %s", summary)
    return summary


def _read_slot() -> datetime:
    db = SessionLocal()
    try:
        return current_slot(demo_now(db))
    finally:
        db.close()


async def scheduler_loop() -> None:
    """재예측 스케줄러 — 15분 슬롯(00/15/30/45) 단위 실행.

    폴링은 그대로 두고(기본 60초) **슬롯이 바뀐 주기에만** 실행한다.
      · 같은 슬롯을 두 번 돌리지 않는다(마지막 실행 슬롯을 기억한다)
      · 슬롯을 놓치지 않는다 — 선택 조건이 "slot <= 현재 슬롯" 이라 밀린 환자가
        다음 실행에 그대로 들어온다(서버 재시작·데모 배속 모두 같은 경로다)
      · 데모 시계를 되돌리면 슬롯이 과거로 가는데, 그때는 due 인 환자가 없어
        아무 일도 하지 않는다(이미 그 시점 행이 있다). 전체 재계산이 필요하면
        POST /api/ed/predictions/run?all=true 를 쓴다.

    ⚠ 슬롯 경계에서 정확히 0초에 도는 것은 아니다. 폴링 주기만큼(기본 ≤60초) 늦게
      실행될 수 있다. 예측 시점 자체는 그대로이므로 값이 달라지지는 않는다.
    """
    interval = settings.predict_scheduler_interval_seconds
    logger.info(
        "재예측 스케줄러 시작 — %d분 슬롯 · 폴링 %.0f초 · 동시성 %d · 대상 app.cohort · %s",
        SLOT_MINUTES, interval, settings.predict_batch_concurrency,
        settings.predict_ai_url,
    )
    unavailable_logged = False
    last_slot: datetime | None = None

    while True:
        try:
            slot = _read_slot()
            if slot != last_slot:
                await _tick(slot)
                # 실패한 환자는 다음 슬롯에서 다시 due 로 잡힌다(행이 없으므로).
                last_slot = slot
            unavailable_logged = False
        except asyncio.CancelledError:
            logger.info("재예측 스케줄러 종료")
            raise
        except Exception:  # noqa: BLE001 — 한 번의 실패로 루프를 죽이지 않는다
            # riskmodel profile 이 꺼져 있으면 매 주기 실패한다. 로그를 도배하지 않는다.
            if not unavailable_logged:
                logger.warning("재예측 주기 실패 — 다음 주기에 다시 시도한다", exc_info=True)
                unavailable_logged = True
        await asyncio.sleep(interval)
