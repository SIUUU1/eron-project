from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else ROOT_DIR / candidate


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    output_dir: Path
    spool_dir: Path
    database_path: Path
    max_upload_bytes: int
    max_audio_duration_seconds: float
    queue_capacity: int
    preload_model: bool
    worker_count: int

    @classmethod
    def from_env(cls) -> "Settings":
        provider = os.getenv("STT_PROVIDER", "groq").strip().lower()
        if provider != "groq":
            raise ValueError("The integrated Whisper service only supports STT_PROVIDER=groq")
        runtime_dir = _env_path("STT_RUNTIME_DIR", ROOT_DIR / "runtime")
        return cls(
            root_dir=ROOT_DIR,
            output_dir=_env_path("STT_OUTPUT_DIR", runtime_dir / "outputs"),
            spool_dir=_env_path("STT_SPOOL_DIR", runtime_dir / "spool"),
            database_path=_env_path("STT_DATABASE_PATH", runtime_dir / "jobs.db"),
            max_upload_bytes=min(
                int(os.getenv("STT_MAX_UPLOAD_MB", "25")),
                25,
            )
            * 1024
            * 1024,
            max_audio_duration_seconds=float(
                os.getenv("STT_MAX_AUDIO_DURATION_SECONDS", "7200")
            ),
            queue_capacity=max(1, int(os.getenv("STT_QUEUE_CAPACITY", "100"))),
            preload_model=_env_bool("STT_PRELOAD_MODEL", True),
            worker_count=max(1, int(os.getenv("STT_CLOUD_WORKERS", "3"))),
        )

    def ensure_directories(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def max_upload_mb(self) -> int:
        return self.max_upload_bytes // (1024 * 1024)
