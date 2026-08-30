from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS transcription_jobs (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    content_type TEXT,
                    size_bytes INTEGER NOT NULL,
                    duration_seconds REAL NOT NULL,
                    audio_path TEXT NOT NULL,
                    result_path TEXT,
                    error_code TEXT
                )
                """
            )

    def create(
        self,
        *,
        job_id: str,
        request_id: str,
        content_type: str | None,
        size_bytes: int,
        duration_seconds: float,
        audio_path: Path,
    ) -> dict[str, Any]:
        created_at = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transcription_jobs (
                    id, request_id, status, created_at, content_type,
                    size_bytes, duration_seconds, audio_path
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    request_id,
                    created_at,
                    content_type,
                    size_bytes,
                    duration_seconds,
                    str(audio_path),
                ),
            )
        return self.get(job_id)  # type: ignore[return-value]

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM transcription_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_status(
        self,
        job_id: str,
        status: str,
        *,
        result_path: Path | None = None,
        error_code: str | None = None,
    ) -> None:
        started_at = utc_now() if status == "processing" else None
        completed_at = utc_now() if status in {"completed", "failed"} else None
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE transcription_jobs
                SET status = ?,
                    started_at = COALESCE(?, started_at),
                    completed_at = COALESCE(?, completed_at),
                    result_path = COALESCE(?, result_path),
                    error_code = ?
                WHERE id = ?
                """,
                (
                    status,
                    started_at,
                    completed_at,
                    str(result_path) if result_path else None,
                    error_code,
                    job_id,
                ),
            )

    def recoverable(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM transcription_jobs
                WHERE status IN ('queued', 'processing')
                ORDER BY created_at ASC
                """
            ).fetchall()
            connection.execute(
                "UPDATE transcription_jobs SET status = 'queued', started_at = NULL "
                "WHERE status = 'processing'"
            )
        return [dict(row) for row in rows]
