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


@dataclass(frozen=True)
class Settings:
    database_url: str

    # nginx 뒤에서는 동일 오리진이라 CORS 가 필요 없다.
    # 컨테이너 밖에서 vite dev 를 띄울 때만 채운다. 기본은 빈 목록.
    cors_origins: list[str] = field(default_factory=list)

    # 위험도 임계값. frontend/src/routes/settings.tsx 의 슬라이더 기본값과 맞춘다.
    risk_critical: float = 0.80
    risk_rising: float = 0.60
    risk_watch: float = 0.30

    # 예측 모델 연동 여부. PREDICT_AI_URL 이 설정되어야 연동으로 본다.
    predict_ai_url: str | None = None

    # 응급기록 초안 생성용 내부 ClinicalNLP 서비스.
    record_ai_url: str | None = None
    clinical_record_ai_timeout_seconds: float = 180.0

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
        risk_critical=_float_env("RISK_THRESHOLD_CRITICAL", 0.80),
        risk_rising=_float_env("RISK_THRESHOLD_RISING", 0.60),
        risk_watch=_float_env("RISK_THRESHOLD_WATCH", 0.30),
        predict_ai_url=os.getenv("PREDICT_AI_URL") or None,
        record_ai_url=os.getenv("RECORD_AI_URL") or None,
        clinical_record_ai_timeout_seconds=_float_env(
            "CLINICAL_RECORD_AI_TIMEOUT_SECONDS",
            180.0,
        ),
        stt_url=os.getenv("STT_URL") or None,
        stt_timeout_seconds=_float_env("STT_TIMEOUT_SECONDS", 300.0),
        stt_poll_interval_seconds=_float_env(
            "STT_POLL_INTERVAL_SECONDS",
            0.5,
        ),
        db_timezone=os.getenv("DB_TIMEZONE") or os.getenv("TZ") or "Asia/Seoul",
    )


settings = load_settings()
