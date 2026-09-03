from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .contracts import validate_whisper_payload


MAX_REQUEST_BYTES = 5 * 1024 * 1024


class ClinicalNlpHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler]):
        super().__init__(server_address, handler)
        self.runtime_executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="clinicalnlp-runtime",
        )

    def server_close(self) -> None:
        super().server_close()
        self.runtime_executor.shutdown(wait=False, cancel_futures=True)


def create_http_server(
    host: str,
    port: int,
    *,
    runtime: Any | None,
    request_timeout_seconds: float,
    unavailable_reason: str = "startup",
    readiness_probe: Callable[[], bool] | None = None,
) -> ClinicalNlpHttpServer:
    """Expose the draft runtime through the internal ClinicalNLP HTTP contract."""

    if request_timeout_seconds <= 0:
        raise ValueError("request_timeout_seconds must be positive")

    safe_unavailable_reason = (
        unavailable_reason
        if unavailable_reason in {"configuration", "assets", "startup"}
        else "startup"
    )

    def runtime_ready() -> bool:
        if runtime is None:
            return False
        if readiness_probe is None:
            return True
        try:
            return readiness_probe() is True
        except Exception:
            return False

    class ClinicalNlpHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                ready = runtime is not None
                payload = {
                    "schema_version": "clinicalnlp-health-v1",
                    "status": "ready" if ready else "unavailable",
                }
                if not ready:
                    payload["reason"] = safe_unavailable_reason
                self._json(200 if ready else 503, payload)
                return
            if path == "/ready":
                ready = runtime_ready()
                payload = {
                    "schema_version": "clinicalnlp-readiness-v1",
                    "status": "ready" if ready else "not_ready",
                }
                if not ready:
                    payload["reason"] = safe_unavailable_reason
                self._json(200 if ready else 503, payload)
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/v2/clinical-workflows":
                self._json(404, {"error": "not_found"})
                return
            if runtime is None:
                self._json(
                    503,
                    {
                        "error": "clinicalnlp_unavailable",
                        "reason": safe_unavailable_reason,
                    },
                )
                return
            if not runtime_ready():
                self._json(
                    503,
                    {
                        "error": "clinicalnlp_unavailable",
                        "reason": "startup",
                    },
                )
                return
            length = int(self.headers.get("content-length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                self._json(400, {"error": "invalid_whisper_payload"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"error": "invalid_whisper_payload"})
                return
            try:
                validate_whisper_payload(payload)
            except ValueError:
                self._json(400, {"error": "invalid_whisper_payload"})
                return
            future = self.server.runtime_executor.submit(  # type: ignore[attr-defined]
                runtime.generate_draft,
                payload,
            )
            try:
                result = future.result(timeout=request_timeout_seconds)
            except FutureTimeoutError:
                future.cancel()
                self._json(504, {"error": "clinicalnlp_timeout"})
                return
            except Exception:
                self._json(503, {"error": "clinicalnlp_unavailable"})
                return
            if (
                not isinstance(result, dict)
                or result.get("schema_version") != "clinical-workflow-v2"
                or result.get("processing_status")
                not in {"completed", "partial", "failed"}
                or result.get("record_status") != "DRAFT"
            ):
                self._json(502, {"error": "invalid_clinicalnlp_response"})
                return
            self._json(200, result)

    return ClinicalNlpHttpServer((host, port), ClinicalNlpHandler)
