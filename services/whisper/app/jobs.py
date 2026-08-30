from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Protocol

from .config import Settings
from .errors import ProviderTranscriptionError
from .job_store import JobStore, utc_now


logger = logging.getLogger("whisper.jobs")


class Transcriber(Protocol):
    @property
    def is_loaded(self) -> bool: ...

    def preload(self) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...

    def transcribe(self, audio_path: Path) -> dict[str, Any]: ...


class JobManager:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        transcriber: Transcriber,
    ) -> None:
        self.settings = settings
        self.store = store
        self.transcriber = transcriber
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._enqueue_lock = asyncio.Lock()
        self.worker_tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        if self.settings.preload_model:
            await asyncio.to_thread(self.transcriber.preload)
        for job in self.store.recoverable():
            audio_path = Path(job["audio_path"])
            if not audio_path.is_file():
                self.store.update_status(
                    job["id"], "failed", error_code="AUDIO_MISSING"
                )
                continue
            self.queue.put_nowait(job["id"])
        self.worker_tasks = [
            asyncio.create_task(self._worker(), name=f"whisper-worker-{index + 1}")
            for index in range(self.settings.worker_count)
        ]

    async def stop(self) -> None:
        for task in self.worker_tasks:
            task.cancel()
        await asyncio.gather(*self.worker_tasks, return_exceptions=True)
        self.worker_tasks = []

    @property
    def is_full(self) -> bool:
        return self.queue.qsize() >= self.settings.queue_capacity

    @property
    def is_running(self) -> bool:
        return bool(self.worker_tasks) and all(
            not task.done() for task in self.worker_tasks
        )

    async def enqueue(self, job: dict[str, Any]) -> bool:
        async with self._enqueue_lock:
            if self.is_full:
                return False
            self.queue.put_nowait(job["id"])
        return True

    async def _worker(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                await self._process(job_id)
            finally:
                self.queue.task_done()

    async def _process(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        audio_path = Path(job["audio_path"])
        result_path = self.settings.output_dir / f"{job_id}.json"
        temporary_path = result_path.with_suffix(".json.tmp")
        self.store.update_status(job_id, "processing")
        try:
            result = await asyncio.to_thread(self.transcriber.transcribe, audio_path)
            document = {
                "api_version": "v1",
                "id": job_id,
                "request_id": job["request_id"],
                "status": "completed",
                "created_at": job["created_at"],
                "completed_at": utc_now(),
                "source": {
                    "content_type": job["content_type"],
                    "size_bytes": job["size_bytes"],
                    "duration_seconds": round(float(job["duration_seconds"]), 3),
                },
                **result,
            }
            temporary_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary_path.replace(result_path)
            self.store.update_status(job_id, "completed", result_path=result_path)
        except ProviderTranscriptionError as exc:
            self.store.update_status(job_id, "failed", error_code=exc.error_code)
            logger.error(
                "transcription_failed job_id=%s error_code=%s",
                job_id,
                exc.error_code,
            )
        except Exception as exc:
            self.store.update_status(
                job_id, "failed", error_code="TRANSCRIPTION_FAILED"
            )
            logger.error(
                "transcription_failed job_id=%s error_type=%s",
                job_id,
                type(exc).__name__,
            )
        finally:
            audio_path.unlink(missing_ok=True)
            temporary_path.unlink(missing_ok=True)
