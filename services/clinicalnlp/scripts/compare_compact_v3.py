from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from clinicalnlp_api3.service import ServiceSettings, build_service_runtime


def _summary(
    result: dict[str, Any],
    *,
    include_field_text: bool,
    mode: str = "compare",
) -> dict[str, Any]:
    comparison = (
        result.get("compact_v3_primary")
        if mode == "primary"
        else result.get("compact_v3_comparison")
    )
    comparison = comparison if isinstance(comparison, dict) else {}
    validation = comparison.get("validation")
    validation = validation if isinstance(validation, dict) else {}
    validation_issues = [
        {
            "issue_code": item.get("issue_code") or item.get("code"),
            "severity": item.get("severity"),
            "fact_id": item.get("fact_id"),
            "field_ids": item.get("field_ids", []),
            "rule_id": item.get("rule_id"),
        }
        for item in validation.get("issues", [])
        if isinstance(item, dict)
    ]
    fields = comparison.get("fields")
    fields = fields if isinstance(fields, dict) else {}
    if mode == "primary":
        record = comparison.get("record")
        fields = (
            record.get("fields")
            if isinstance(record, dict) and isinstance(record.get("fields"), dict)
            else {}
        )
    field_summary: dict[str, Any] = {}
    for field_id, field in fields.items():
        if not isinstance(field, dict):
            continue
        if mode == "primary":
            item = {
                "generation_status": field.get("generation_status"),
                "fact_refs": field.get("fact_refs", []),
            }
            if include_field_text:
                item["text"] = field.get("text")
        else:
            item = {
                "matches": field.get("matches") is True,
                "comparison_class": field.get("comparison_class"),
                "v2_segment_ids": field.get("v2_segment_ids", []),
                "compact_v3_segment_ids": field.get(
                    "compact_v3_segment_ids", []
                ),
            }
            if include_field_text:
                item["v2_text"] = field.get("v2_text")
                item["compact_v3_text"] = field.get("compact_v3_text")
        field_summary[str(field_id)] = item
    return {
        "schema_version": "clinical-record-compact-evaluation-v1",
        "authoritative_v2": {
            "processing_status": result.get("processing_status"),
            "error_codes": [
                item.get("code")
                for item in result.get("errors", [])
                if isinstance(item, dict) and isinstance(item.get("code"), str)
            ],
            "telemetry": result.get("telemetry", {}),
        },
        "compact_v3": {
            "status": comparison.get("status", "missing"),
            "prompt_version": comparison.get("prompt_version"),
            "elapsed_ms": comparison.get("elapsed_ms"),
            "candidate_snapshot_count": comparison.get(
                "candidate_snapshot_count", 0
            ),
            "validation_status": validation.get("status"),
            "processing_status": validation.get("processing_status"),
            "issue_count": (validation.get("summary") or {}).get("issue_count")
            if isinstance(validation.get("summary"), dict)
            else None,
            "issues": validation_issues,
            "mismatch_field_ids": comparison.get("mismatch_field_ids", []),
            "evidence_mismatch_field_ids": comparison.get(
                "evidence_mismatch_field_ids", []
            ),
            "fields": field_summary,
            "error_code": comparison.get("error_code"),
            "detail": comparison.get("detail"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one local v2 versus Compact v3 ClinicalNLP comparison."
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument(
        "--mode",
        choices=("compare", "primary"),
        default="compare",
    )
    parser.add_argument("--include-field-text", action="store_true")
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("input must contain one Whisper JSON object")

    environment = dict(os.environ)
    environment["CLINICALNLP_COMPACT_V3_MODE"] = args.mode
    settings = ServiceSettings.from_mapping(environment)
    bundle = build_service_runtime(settings)
    started = time.perf_counter()
    try:
        result = bundle.runtime.generate_draft(payload)
    finally:
        bundle.close()
    summary = _summary(
        result,
        include_field_text=args.include_field_text,
        mode=args.mode,
    )
    summary["total_wall_ms"] = round((time.perf_counter() - started) * 1000, 3)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
