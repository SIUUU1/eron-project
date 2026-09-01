"""환경변수 집약. 값을 코드에 하드코딩하지 않는다."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv_env(key: str) -> list[str]:
    raw = os.getenv(key, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _float_env(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bool_env(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str

    # nginx 뒤에서는 동일 오리진이라 CORS 가 필요 없다.
    # 컨테이너 밖에서 vite dev 를 띄울 때만 채운다. 기본은 빈 목록.
    cors_origins: list[str] = field(default_factory=list)

    # 위험도 등급 경계. artifacts/bundle.json["risk_bands"] 의 **실측 경계**다.
    #
    # 모델은 3구간(🟢저위험 / 🟡악화 / 🔴매우악화)이고 화면은 4단계라, 🔴 을
    # rising / critical 로 한 번 더 나눈다.
    #   risk_watch    = bundle risk_bands.thresholds.amber (운영점 recall_85)
    #   risk_rising   = bundle risk_bands.thresholds.red   (운영점 recall_70)
    #   risk_critical = 🔴 구간 상위 세분
    #
    # ⚠ 옛 기본값 0.30/0.60/0.80 을 쓰면 거의 전원이 stable 로 표시된다.
    #   보정 확률의 93.7% 가 0.0358 미만이기 때문이다.
    risk_critical: float = 0.40
    risk_rising: float = 0.133371
    risk_watch: float = 0.035838

    # 예측 모델 연동 여부. PREDICT_AI_URL 이 설정되어야 연동으로 본다.
    predict_ai_url: str | None = None
    predict_ai_timeout_seconds: float = 60.0

    # 재예측 스케줄러. 데모 시계가 흐르면 새 예측 시점이 생기므로 주기적으로 따라간다.
    predict_scheduler_enabled: bool = True
    predict_scheduler_interval_seconds: float = 60.0
    # 한 슬롯의 환자를 riskmodel 로 보낼 때의 동시 요청 수.
    # riskmodel 은 uvicorn 워커 1개라 무제한으로 던지면 타임아웃이 난다.
    predict_batch_concurrency: int = 4

    # 응급기록 초안 생성용 내부 ClinicalNLP 서비스.
    record_ai_url: str | None = None
    clinical_record_ai_timeout_seconds: float = 180.0

    # KCD 검색어를 표준 진단명으로 확장하는 읽기 전용 임상용어 약어 사전.
    kcd_alias_db_path: str | None = None

    # 음성 파일을 Whisper JSON으로 변환하는 내부 비동기 STT 서비스.
    stt_url: str | None = None
    stt_timeout_seconds: float = 300.0
    stt_poll_interval_seconds: float = 0.5

    # DB 세션 타임존.
    # PostgreSQL 서버 기본값은 Etc/UTC 이고, 컨테이너의 TZ 는 세션에 전달되지 않는다.
    # 명시하지 않으면 API 가 돌려주는 데모 시각이 실제 시각보다 9시간 뒤처진다.
    db_timezone: str = "Asia/Seoul"

    @property
    def model_connected(self) -> bool:
        return bool(self.predict_ai_url)


def load_settings() -> Settings:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")

    return Settings(
        database_url=database_url,
        cors_origins=_csv_env("CORS_ORIGINS"),
        # RISK_* 가 정식 이름이다. RISK_THRESHOLD_* 는 옛 이름으로, 이미 쓰고 있는
        # 배포가 조용히 기본값으로 되돌아가지 않도록 폴백으로만 남긴다.
        risk_critical=_float_env("RISK_CRITICAL", _float_env("RISK_THRESHOLD_CRITICAL", 0.40)),
        risk_rising=_float_env("RISK_RISING", _float_env("RISK_THRESHOLD_RISING", 0.133371)),
        risk_watch=_float_env("RISK_WATCH", _float_env("RISK_THRESHOLD_WATCH", 0.035838)),
        predict_ai_url=os.getenv("PREDICT_AI_URL") or None,
        predict_ai_timeout_seconds=_float_env("PREDICT_AI_TIMEOUT_SECONDS", 60.0),
        predict_scheduler_enabled=_bool_env("PREDICT_SCHEDULER_ENABLED", True),
        predict_batch_concurrency=int(_float_env("PREDICT_BATCH_CONCURRENCY", 4)),
        predict_scheduler_interval_seconds=_float_env(
            "PREDICT_SCHEDULER_INTERVAL_SECONDS",
            60.0,
        ),
        record_ai_url=os.getenv("RECORD_AI_URL") or None,
        clinical_record_ai_timeout_seconds=_float_env(
            "CLINICAL_RECORD_AI_TIMEOUT_SECONDS",
            180.0,
        ),
        kcd_alias_db_path=os.getenv("KCD_ALIAS_DB_PATH") or None,
        stt_url=os.getenv("STT_URL") or None,
        stt_timeout_seconds=_float_env("STT_TIMEOUT_SECONDS", 300.0),
        stt_poll_interval_seconds=_float_env(
            "STT_POLL_INTERVAL_SECONDS",
            0.5,
        ),
        db_timezone=os.getenv("DB_TIMEZONE") or os.getenv("TZ") or "Asia/Seoul",
    )


settings = load_settings()
