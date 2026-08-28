from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from clinicalnlp_api3.medical_span_contract import (  # noqa: E402
    normalize_translated_segments,
    validate_scispacy_spans,
)
from clinicalnlp_api3.medical_span_worker import WORKER_PROTOCOL  # noqa: E402
from clinicalnlp_api3.scispacy_runtime import ScispacyUmlsExtractor  # noqa: E402


def _write(payload: dict[str, Any]) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Persistent local scispaCy/UMLS extraction worker"
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=PROJECT_ROOT / "runtime" / "scispacy" / "cache",
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    try:
        extractor = ScispacyUmlsExtractor(cache_root=args.cache_root)
    except Exception as error:
        _write(
            {
                "protocol": WORKER_PROTOCOL,
                "type": "initialization_error",
                "error_type": type(error).__name__,
            }
        )
        return 1

    _write(
        {
            "protocol": WORKER_PROTOCOL,
            "type": "ready",
            "extractor": extractor.metadata,
        }
    )

    for line in sys.stdin:
        request: object = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("worker request must be an object")
            if request.get("protocol") != WORKER_PROTOCOL:
                raise ValueError("unsupported worker protocol")
            if request.get("type") == "shutdown":
                _write(
                    {
                        "protocol": WORKER_PROTOCOL,
                        "type": "shutdown_ack",
                    }
                )
                return 0
            if request.get("type") != "extract":
                raise ValueError("unsupported worker request type")
            request_id = request.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                raise ValueError("worker request_id is required")
            segments = normalize_translated_segments(
                request.get("translated_segments")
            )
            extracted_spans, metadata = extractor.extract(segments)
            spans = validate_scispacy_spans(segments, extracted_spans)
            _write(
                {
                    "protocol": WORKER_PROTOCOL,
                    "type": "result",
                    "request_id": request_id,
                    "ok": True,
                    "spans": spans,
                    "extractor": metadata,
                }
            )
        except Exception as error:
            _write(
                {
                    "protocol": WORKER_PROTOCOL,
                    "type": "result",
                    "request_id": (
                        request.get("request_id")
                        if isinstance(request, dict)
                        else None
                    ),
                    "ok": False,
                    "error_type": type(error).__name__,
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

