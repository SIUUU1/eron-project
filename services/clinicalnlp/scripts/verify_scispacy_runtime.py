from __future__ import annotations

import argparse
import json
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any


REQUIRED_DATASET_SUFFIXES = (
    ".nmslib_index.bin",
    ".tfidf_vectorizer.joblib",
    ".tfidf_vectors_sparse.npz",
    ".concept_aliases.json",
    ".umls_2022_ab_cat0129.jsonl",
    ".umls_semantic_type_tree.tsv",
)


def inspect_layout(runtime_root: Path) -> dict[str, object]:
    runtime_root = runtime_root.resolve()
    datasets = runtime_root / "cache" / "datasets"
    missing: list[str] = []
    duplicates: list[str] = []

    python_path = runtime_root / ".venv" / "bin" / "python"
    if not python_path.is_file():
        missing.append(".venv/bin/python")

    for suffix in REQUIRED_DATASET_SUFFIXES:
        matches = (
            [
                path
                for path in datasets.glob(f"*{suffix}")
                if path.is_file() and not path.name.endswith(f"{suffix}.json")
            ]
            if datasets.is_dir()
            else []
        )
        relative_pattern = f"cache/datasets/*{suffix}"
        if not matches:
            missing.append(relative_pattern)
        elif len(matches) > 1:
            duplicates.append(relative_pattern)

    ready = not missing and not duplicates
    return {
        "schema_version": "eron-scispacy-runtime-check-v1",
        "status": "ready" if ready else "not_ready",
        "runtime_root": str(runtime_root),
        "missing": missing,
        "duplicates": duplicates,
    }


def _readline_with_timeout(
    stream: Any,
    *,
    timeout_seconds: float,
) -> str:
    messages: queue.Queue[str] = queue.Queue(maxsize=1)
    reader = threading.Thread(target=lambda: messages.put(stream.readline()), daemon=True)
    reader.start()
    try:
        return messages.get(timeout=timeout_seconds)
    except queue.Empty as error:
        raise TimeoutError("worker response timed out") from error


def verify_worker(
    *,
    python_path: Path,
    worker_path: Path,
    cache_root: Path,
    timeout_seconds: float,
) -> tuple[bool, dict[str, object]]:
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            [
                str(python_path),
                str(worker_path),
                "--cache-root",
                str(cache_root),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if process.stdout is None or process.stdin is None:
            raise RuntimeError("worker pipes are unavailable")
        ready_line = _readline_with_timeout(
            process.stdout,
            timeout_seconds=timeout_seconds,
        )
        ready = json.loads(ready_line)
        if (
            not isinstance(ready, dict)
            or ready.get("protocol") != "scispacy-umls-worker-v1"
            or ready.get("type") != "ready"
        ):
            return False, {"worker_error": "worker_not_ready"}

        process.stdin.write(
            json.dumps(
                {"protocol": "scispacy-umls-worker-v1", "type": "shutdown"},
                separators=(",", ":"),
            )
            + "\n"
        )
        process.stdin.flush()
        acknowledgement = json.loads(
            _readline_with_timeout(process.stdout, timeout_seconds=10)
        )
        if acknowledgement.get("type") != "shutdown_ack":
            return False, {"worker_error": "shutdown_not_acknowledged"}
        process.wait(timeout=10)
        return True, {"extractor": ready.get("extractor") or {}}
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError, TimeoutError) as error:
        return False, {"worker_error": type(error).__name__}
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the external Linux scispaCy/UMLS runtime"
    )
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument(
        "--layout-only",
        action="store_true",
        help="Check files without starting the UMLS worker",
    )
    parser.add_argument("--python-path", type=Path)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()

    report = inspect_layout(args.runtime_root)
    if report["status"] != "ready":
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 1
    if args.layout_only:
        report["verification"] = "layout"
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 0

    runtime_root = args.runtime_root.resolve()
    python_path = (args.python_path or runtime_root / ".venv" / "bin" / "python").resolve()
    worker_path = (
        args.worker or Path(__file__).resolve().parent / "medical_span_worker.py"
    ).resolve()
    ready, worker_report = verify_worker(
        python_path=python_path,
        worker_path=worker_path,
        cache_root=runtime_root / "cache",
        timeout_seconds=args.timeout,
    )
    report.update(worker_report)
    report["status"] = "ready" if ready else "not_ready"
    report["verification"] = "worker_protocol"
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
