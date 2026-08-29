from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from verify_scispacy_runtime import REQUIRED_DATASET_SUFFIXES


SERVICE_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_PATH = SERVICE_ROOT / "scispacy-requirements.txt"
DEFAULT_IMAGE = "python:3.12-slim"


class SetupError(RuntimeError):
    pass


def inspect_cache_source(cache_source: Path) -> dict[str, Path]:
    datasets = cache_source.resolve() / "datasets"
    if not datasets.is_dir():
        raise SetupError("cache source must contain a datasets directory")

    resolved: dict[str, Path] = {}
    for suffix in REQUIRED_DATASET_SUFFIXES:
        matches = [
            path
            for path in datasets.glob(f"*{suffix}")
            if path.is_file() and not path.name.endswith(f"{suffix}.json")
        ]
        if len(matches) != 1:
            raise SetupError(
                f"expected exactly one cache/datasets/*{suffix}, found {len(matches)}"
            )
        resolved[suffix] = matches[0]
    return resolved


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, stdout=sys.stderr, stderr=sys.stderr)
    if completed.returncode != 0:
        raise SetupError(f"command failed with exit code {completed.returncode}")


def _docker_base(
    *,
    docker_command: str,
    image: str,
    runtime_root: Path,
) -> list[str]:
    return [
        docker_command,
        "run",
        "--rm",
        "--volume",
        f"{runtime_root.resolve()}:/runtime/scispacy",
        image,
    ]


def install_runtime(
    *,
    runtime_root: Path,
    cache_source: Path,
    docker_command: str,
    image: str,
) -> dict[str, object]:
    inspect_cache_source(cache_source)
    runtime_root = runtime_root.resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    target_cache = runtime_root / "cache"
    if target_cache.exists():
        inspect_cache_source(target_cache)
        cache_status = "reused"
    else:
        shutil.copytree(cache_source.resolve(), target_cache)
        cache_status = "copied"

    base = _docker_base(
        docker_command=docker_command,
        image=image,
        runtime_root=runtime_root,
    )
    _run(
        base
        + [
            "python",
            "-m",
            "venv",
            "--copies",
            "/runtime/scispacy/.venv",
        ]
    )
    _run(
        [
            docker_command,
            "run",
            "--rm",
            "--volume",
            f"{runtime_root}:/runtime/scispacy",
            "--volume",
            f"{REQUIREMENTS_PATH.resolve()}:/tmp/scispacy-requirements.txt:ro",
            image,
            "/runtime/scispacy/.venv/bin/python",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--requirement",
            "/tmp/scispacy-requirements.txt",
        ]
    )
    verifier = subprocess.run(
        [
            docker_command,
            "run",
            "--rm",
            "--volume",
            f"{runtime_root}:/runtime/scispacy:ro",
            "--volume",
            f"{SERVICE_ROOT.resolve()}:/app:ro",
            image,
            "python",
            "/app/scripts/verify_scispacy_runtime.py",
            "--runtime-root",
            "/runtime/scispacy",
            "--worker",
            "/app/scripts/medical_span_worker.py",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if verifier.returncode != 0:
        raise SetupError(f"worker verification failed: {verifier.stdout.strip()}")
    verification = json.loads(verifier.stdout)
    return {
        "schema_version": "eron-scispacy-runtime-setup-v1",
        "status": "ready",
        "cache": cache_status,
        "verification": verification,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a Linux scispaCy/UMLS runtime through Docker"
    )
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--cache-source", type=Path, required=True)
    parser.add_argument("--docker-command", default="docker")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()

    try:
        datasets = inspect_cache_source(args.cache_source)
        if args.check_only:
            report: dict[str, object] = {
                "schema_version": "eron-scispacy-runtime-setup-v1",
                "status": "source_ready",
                "dataset_count": len(datasets),
            }
        else:
            report = install_runtime(
                runtime_root=args.runtime_root,
                cache_source=args.cache_source,
                docker_command=args.docker_command,
                image=args.image,
            )
    except (OSError, SetupError, json.JSONDecodeError) as error:
        report = {
            "schema_version": "eron-scispacy-runtime-setup-v1",
            "status": "not_ready",
            "error_type": type(error).__name__,
            "message": str(error),
        }
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 1

    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
