from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence

from .medical_span_contract import (
    normalize_translated_segments,
    validate_scispacy_spans,
)


WORKER_PROTOCOL = "scispacy-umls-worker-v1"
WorkerLane = Literal["clinical", "experiment"]


@dataclass(frozen=True, slots=True)
class MedicalSpanLinkOutcome:
    status: Literal["linked", "fallback"]
    spans: tuple[dict[str, Any], ...]
    extractor: Mapping[str, Any]
    generation: int | None
    fallback_reason: str | None = None

    @property
    def fallback_used(self) -> bool:
        return self.status == "fallback"


class MedicalSpanWorker:
    """Own one reusable scispaCy/UMLS subprocess behind a small local API."""

    def __init__(
        self,
        project_root: Path,
        *,
        timeout_seconds: float = 90.0,
        python_path: Path | None = None,
        worker_path: Path | None = None,
        cache_root: Path | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._project_root = Path(project_root).resolve()
        self._timeout_seconds = float(timeout_seconds)
        self._python_path = Path(
            python_path or self._resolve_python_path()
        ).resolve()
        self._worker_path = Path(
            worker_path or self._project_root / "scripts" / "medical_span_worker.py"
        ).resolve()
        self._cache_root = Path(
            cache_root or self._project_root / "runtime" / "scispacy" / "cache"
        ).resolve()

        self._state_lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._schedule = threading.Condition()
        self._cleanup_lock = threading.Lock()
        self._cleanup_threads: set[threading.Thread] = set()
        self._active_lane: WorkerLane | None = None
        self._clinical_waiters = 0
        self._experiment_job_active = False
        self._ready_event = threading.Event()
        self._messages: queue.Queue[tuple[int, dict[str, Any]]] = queue.Queue()
        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._generation = 0
        self._state = "not_started"
        self._extractor: dict[str, Any] = {}
        self._last_error: str | None = None
        self._startup_failure_reason: str | None = None
        self._closed = False

    def start(self) -> None:
        """Start loading the heavy runtime without waiting for readiness."""

        self._ensure_started()

    def link(
        self,
        translated_segments: Sequence[Mapping[str, Any]],
        *,
        lane: WorkerLane = "clinical",
    ) -> MedicalSpanLinkOutcome:
        if lane not in {"clinical", "experiment"}:
            raise ValueError("lane must be 'clinical' or 'experiment'")
        with self._state_lock:
            if self._closed:
                return self._fallback("closed")
        segments = normalize_translated_segments(list(translated_segments))
        deadline = time.monotonic() + self._timeout_seconds

        if lane == "experiment":
            admission_failure = self._begin_experiment()
            if admission_failure is not None:
                return self._fallback(admission_failure)
            try:
                return self._link_experiment(segments, deadline)
            finally:
                self._end_experiment()

        if not self._acquire_slice("clinical", deadline):
            with self._state_lock:
                reason = "closed" if self._closed else "deadline_exceeded"
            return self._fallback(reason)
        try:
            return self._link_batch(segments, deadline)
        finally:
            self._release_slice()

    def _link_experiment(
        self,
        segments: list[dict[str, Any]],
        deadline: float,
    ) -> MedicalSpanLinkOutcome:
        combined_spans: list[dict[str, Any]] = []
        extractor: Mapping[str, Any] = MappingProxyType({})
        generation: int | None = None
        extraction_latency_ms = 0.0
        has_complete_latency = True
        worker_request_count = 0
        for segment in segments:
            if not self._acquire_slice("experiment", deadline):
                with self._state_lock:
                    reason = "closed" if self._closed else "deadline_exceeded"
                return self._fallback(reason, generation=generation)
            try:
                outcome = self._link_batch([segment], deadline)
            finally:
                self._release_slice()
            if outcome.fallback_used:
                return outcome
            if generation is not None and outcome.generation != generation:
                return self._fallback("worker_crash", generation=outcome.generation)
            generation = outcome.generation
            extractor = outcome.extractor
            worker_request_count += 1
            segment_latency = extractor.get("extraction_latency_ms")
            if (
                isinstance(segment_latency, (int, float))
                and not isinstance(segment_latency, bool)
                and math.isfinite(segment_latency)
                and segment_latency >= 0
            ):
                extraction_latency_ms += float(segment_latency)
            else:
                has_complete_latency = False
            combined_spans.extend(outcome.spans)
        combined_extractor = dict(extractor)
        combined_extractor["worker_request_count"] = worker_request_count
        if has_complete_latency:
            combined_extractor["extraction_latency_ms"] = round(
                extraction_latency_ms,
                3,
            )
        else:
            combined_extractor.pop("extraction_latency_ms", None)
        return MedicalSpanLinkOutcome(
            status="linked",
            spans=tuple(combined_spans),
            extractor=MappingProxyType(combined_extractor),
            generation=generation,
        )

    def _link_batch(
        self,
        segments: list[dict[str, Any]],
        deadline: float,
    ) -> MedicalSpanLinkOutcome:

        with self._request_lock:
            generation = self._ensure_started()
            if generation is None:
                return self._fallback("worker_unavailable")

            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._ready_event.wait(remaining):
                with self._state_lock:
                    if self._closed:
                        return self._fallback("closed", generation=generation)
                self._fail_generation(generation, "worker deadline exceeded")
                return self._fallback("deadline_exceeded", generation=generation)
            with self._state_lock:
                startup_failure_reason = self._startup_failure_reason
                ready = (
                    self._state == "ready"
                    and self._process is not None
                    and self._process.poll() is None
                )
            if startup_failure_reason is not None:
                self._fail_generation(generation, "worker startup protocol failed")
                return self._fallback(
                    startup_failure_reason,
                    generation=generation,
                )
            if not ready:
                with self._state_lock:
                    if self._closed:
                        return self._fallback("closed", generation=generation)
                self._fail_generation(generation, "worker process exited")
                return self._fallback("worker_crash", generation=generation)

            request_id = uuid.uuid4().hex
            request = {
                "protocol": WORKER_PROTOCOL,
                "type": "extract",
                "request_id": request_id,
                "translated_segments": segments,
            }
            with self._state_lock:
                process = self._process
                process_unavailable = (
                    process is None
                    or process.poll() is not None
                    or process.stdin is None
                    or generation != self._generation
                )
            if process_unavailable or process is None or process.stdin is None:
                with self._state_lock:
                    if self._closed:
                        return self._fallback("closed", generation=generation)
                self._fail_generation(generation, "worker process exited")
                return self._fallback("worker_crash", generation=generation)

            write_finished = threading.Event()
            write_failed = threading.Event()

            def write_request() -> None:
                try:
                    process.stdin.write(
                        json.dumps(request, ensure_ascii=False) + "\n"
                    )
                    process.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    write_failed.set()
                finally:
                    write_finished.set()

            writer_thread = threading.Thread(
                target=write_request,
                name=f"medical-span-writer-{generation}",
                daemon=True,
            )
            writer_thread.start()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not write_finished.wait(remaining):
                with self._state_lock:
                    if self._closed:
                        return self._fallback("closed", generation=generation)
                self._fail_generation(generation, "worker deadline exceeded")
                return self._fallback("deadline_exceeded", generation=generation)
            if write_failed.is_set():
                with self._state_lock:
                    if self._closed:
                        return self._fallback("closed", generation=generation)
                self._fail_generation(generation, "worker pipe failed")
                return self._fallback("worker_crash", generation=generation)

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    with self._state_lock:
                        if self._closed:
                            return self._fallback("closed", generation=generation)
                    self._fail_generation(generation, "worker deadline exceeded")
                    return self._fallback(
                        "deadline_exceeded", generation=generation
                    )
                try:
                    message_generation, message = self._messages.get(timeout=remaining)
                except queue.Empty:
                    with self._state_lock:
                        if self._closed:
                            return self._fallback("closed", generation=generation)
                    self._fail_generation(generation, "worker deadline exceeded")
                    return self._fallback(
                        "deadline_exceeded", generation=generation
                    )
                if message_generation != generation:
                    continue
                if message.get("type") == "closed":
                    return self._fallback("closed", generation=generation)
                if message.get("type") == "worker_exit":
                    self._fail_generation(generation, "worker process exited")
                    return self._fallback("worker_crash", generation=generation)
                if message.get("type") == "protocol_error" or (
                    message.get("type") != "result"
                    or message.get("request_id") != request_id
                ):
                    self._fail_generation(generation, "invalid worker protocol")
                    return self._fallback("protocol_error", generation=generation)
                if message.get("ok") is not True:
                    reason = (
                        "worker_error"
                        if message.get("ok") is False
                        else "protocol_error"
                    )
                    error = (
                        "worker extraction failed"
                        if reason == "worker_error"
                        else "invalid worker protocol"
                    )
                    self._fail_generation(generation, error)
                    return self._fallback(reason, generation=generation)
                extractor = message.get("extractor")
                spans = message.get("spans")
                if not isinstance(extractor, dict) or not isinstance(spans, list):
                    self._fail_generation(generation, "invalid worker protocol")
                    return self._fallback("protocol_error", generation=generation)
                try:
                    spans = validate_scispacy_spans(segments, spans)
                except Exception:
                    self._fail_generation(generation, "invalid worker protocol")
                    return self._fallback("protocol_error", generation=generation)
                return MedicalSpanLinkOutcome(
                    status="linked",
                    spans=tuple(item for item in spans if isinstance(item, dict)),
                    extractor=MappingProxyType(dict(extractor)),
                    generation=generation,
                )

    def _begin_experiment(self) -> str | None:
        with self._schedule:
            if self._closed:
                return "closed"
            if self._experiment_job_active or self._active_lane == "experiment":
                return "experiment_busy"
            if self._active_lane == "clinical" or self._clinical_waiters:
                return "clinical_priority"
            self._experiment_job_active = True
            return None

    def _end_experiment(self) -> None:
        with self._schedule:
            self._experiment_job_active = False
            self._schedule.notify_all()

    def _acquire_slice(self, lane: WorkerLane, deadline: float) -> bool:
        with self._schedule:
            if lane == "clinical":
                self._clinical_waiters += 1
            try:
                while self._active_lane is not None or (
                    lane == "experiment" and self._clinical_waiters > 0
                ):
                    if self._closed:
                        return False
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._schedule.wait(remaining)
                if self._closed:
                    return False
                self._active_lane = lane
                return True
            finally:
                if lane == "clinical":
                    self._clinical_waiters -= 1

    def _release_slice(self) -> None:
        with self._schedule:
            self._active_lane = None
            self._schedule.notify_all()

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            available = self._is_installed()
            return {
                "schema_version": "medical-span-worker-status-v1",
                "available": available,
                "state": self._state if available else "unavailable",
                "ready": available and self._state == "ready",
                "generation": self._generation or None,
                "extractor": dict(self._extractor),
                "last_error": self._last_error,
            }

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._state = "stopped"
            process = self._process
            reader_thread = self._reader_thread
            generation = self._generation
            self._process = None
            self._reader_thread = None
            self._ready_event.set()
            if generation:
                self._messages.put(
                    (
                        generation,
                        {"protocol": WORKER_PROTOCOL, "type": "closed"},
                    )
                )
        with self._schedule:
            self._schedule.notify_all()

        request_idle = self._request_lock.acquire(blocking=False)
        if process is not None and process.poll() is None and request_idle:
            try:
                if process.stdin is not None:
                    process.stdin.write(
                        json.dumps(
                            {"protocol": WORKER_PROTOCOL, "type": "shutdown"}
                        )
                        + "\n"
                    )
                    process.stdin.flush()
                    process.stdin.close()
                process.wait(timeout=1)
            except (BrokenPipeError, OSError, ValueError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        elif process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        if request_idle:
            self._request_lock.release()
        if reader_thread is not None and reader_thread is not threading.current_thread():
            reader_thread.join(timeout=1)
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
            if process.stdout is not None and not process.stdout.closed:
                process.stdout.close()
        self._join_cleanup_threads()

    def _ensure_started(self) -> int | None:
        with self._state_lock:
            if self._closed:
                return None
            if self._process is not None and self._process.poll() is None:
                return self._generation
            if self._process is not None:
                stale_process = self._process
                stale_reader = self._reader_thread
                self._process = None
                self._reader_thread = None
                self._schedule_failed_process_cleanup(
                    stale_process,
                    stale_reader,
                )
            if not self._is_installed():
                self._state = "failed"
                self._last_error = "scispaCy worker runtime is not installed"
                return None

            self._ready_event.clear()
            self._extractor = {}
            self._last_error = None
            self._startup_failure_reason = None
            self._generation += 1
            generation = self._generation
            creation_flags = (
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            try:
                process = subprocess.Popen(
                    [
                        str(self._python_path),
                        str(self._worker_path),
                        "--cache-root",
                        str(self._cache_root),
                    ],
                    cwd=self._project_root,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creation_flags,
                )
            except OSError as exc:
                self._state = "failed"
                self._last_error = f"worker start failed: {type(exc).__name__}"
                return None
            self._process = process
            self._state = "starting"
            thread = threading.Thread(
                target=self._read_output,
                args=(process, generation),
                name=f"medical-span-worker-{generation}",
                daemon=True,
            )
            self._reader_thread = thread
            thread.start()
            return generation

    def _resolve_python_path(self) -> Path:
        runtime_root = self._project_root / "runtime" / "scispacy" / ".venv"
        candidates = (
            runtime_root / "Scripts" / "python.exe",
            runtime_root / "bin" / "python",
        )
        return next((path for path in candidates if path.is_file()), candidates[0])

    def _is_installed(self) -> bool:
        return (
            self._python_path.is_file()
            and self._worker_path.is_file()
            and (self._cache_root / "datasets").is_dir()
        )

    def _read_output(
        self,
        process: subprocess.Popen[str],
        generation: int,
    ) -> None:
        stdout = process.stdout
        if stdout is None:
            return
        for raw_line in stdout:
            try:
                message = json.loads(raw_line)
            except json.JSONDecodeError:
                self._messages.put(
                    (
                        generation,
                        {"protocol": WORKER_PROTOCOL, "type": "protocol_error"},
                    )
                )
                self._signal_startup_failure(
                    process,
                    generation,
                    reason="protocol_error",
                    error="invalid worker protocol",
                )
                continue
            if not isinstance(message, dict) or message.get("protocol") != WORKER_PROTOCOL:
                self._messages.put(
                    (
                        generation,
                        {"protocol": WORKER_PROTOCOL, "type": "protocol_error"},
                    )
                )
                self._signal_startup_failure(
                    process,
                    generation,
                    reason="protocol_error",
                    error="invalid worker protocol",
                )
                continue
            if message.get("type") == "ready":
                with self._state_lock:
                    if self._process is process and self._generation == generation:
                        extractor = message.get("extractor")
                        self._extractor = (
                            dict(extractor) if isinstance(extractor, dict) else {}
                        )
                        self._state = "ready"
                        self._ready_event.set()
                continue
            if message.get("type") == "initialization_error":
                self._signal_startup_failure(
                    process,
                    generation,
                    reason="worker_error",
                    error="worker initialization failed",
                )
            else:
                self._signal_startup_failure(
                    process,
                    generation,
                    reason="protocol_error",
                    error="invalid worker startup protocol",
                )
            self._messages.put((generation, message))

        failed_process: subprocess.Popen[str] | None = None
        failed_reader: threading.Thread | None = None
        with self._state_lock:
            if (
                self._process is process
                and self._generation == generation
                and not self._closed
            ):
                self._state = "failed"
                self._last_error = "worker process exited"
                self._ready_event.set()
                failed_process = self._process
                failed_reader = self._reader_thread
                self._process = None
                self._reader_thread = None
                self._messages.put(
                    (
                        generation,
                        {"protocol": WORKER_PROTOCOL, "type": "worker_exit"},
                    )
                )
        if failed_process is not None or failed_reader is not None:
            self._schedule_failed_process_cleanup(failed_process, failed_reader)

    def _signal_startup_failure(
        self,
        process: subprocess.Popen[str],
        generation: int,
        *,
        reason: str,
        error: str,
    ) -> None:
        with self._state_lock:
            if (
                self._process is process
                and self._generation == generation
                and self._state == "starting"
            ):
                self._startup_failure_reason = reason
                self._state = "failed"
                self._last_error = error
                self._ready_event.set()

    def _fallback(
        self,
        reason: str,
        *,
        generation: int | None = None,
    ) -> MedicalSpanLinkOutcome:
        with self._state_lock:
            extractor = dict(self._extractor)
            current_generation = generation or (self._generation or None)
        return MedicalSpanLinkOutcome(
            status="fallback",
            spans=(),
            extractor=MappingProxyType(extractor),
            generation=current_generation,
            fallback_reason=reason,
        )

    def _fail_generation(self, generation: int, error: str) -> None:
        with self._state_lock:
            if self._generation != generation or self._closed:
                return
            process = self._process
            reader_thread = self._reader_thread
            self._process = None
            self._reader_thread = None
            self._state = "failed"
            self._last_error = error
            self._ready_event.set()

        self._schedule_failed_process_cleanup(process, reader_thread)

    def _schedule_failed_process_cleanup(
        self,
        process: subprocess.Popen[str] | None,
        reader_thread: threading.Thread | None,
    ) -> None:
        if process is None and reader_thread is None:
            return

        def clean_up() -> None:
            try:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
                if reader_thread is not None and (
                    reader_thread is not threading.current_thread()
                ):
                    reader_thread.join(timeout=1)
                if process is not None:
                    if process.stdin is not None and not process.stdin.closed:
                        try:
                            process.stdin.close()
                        except (OSError, ValueError):
                            pass
                    if process.stdout is not None and not process.stdout.closed:
                        process.stdout.close()
            finally:
                with self._cleanup_lock:
                    self._cleanup_threads.discard(threading.current_thread())

        cleanup_thread = threading.Thread(
            target=clean_up,
            name=f"medical-span-cleanup-{self._generation}",
            daemon=True,
        )
        with self._cleanup_lock:
            self._cleanup_threads.add(cleanup_thread)
            # Publish the thread only after start() has been called while holding
            # the same lock used by _join_cleanup_threads. Otherwise close() can
            # observe the new Thread object before it has started and join() raises
            # ``RuntimeError: cannot join thread before it is started``.
            cleanup_thread.start()

    def _join_cleanup_threads(self) -> None:
        with self._cleanup_lock:
            threads = tuple(self._cleanup_threads)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=3)

