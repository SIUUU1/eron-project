from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).parents[1]
VERIFY_SCRIPT = SERVICE_ROOT / "scripts" / "verify_scispacy_runtime.py"
SETUP_SCRIPT = SERVICE_ROOT / "scripts" / "setup_scispacy_runtime.py"
REQUIREMENTS_PATH = SERVICE_ROOT / "scispacy-requirements.txt"
DATASET_SUFFIXES = (
    ".nmslib_index.bin",
    ".tfidf_vectorizer.joblib",
    ".tfidf_vectors_sparse.npz",
    ".concept_aliases.json",
    ".umls_2022_ab_cat0129.jsonl",
    ".umls_semantic_type_tree.tsv",
)


class ScispacyRuntimeSetupTests(unittest.TestCase):
    @staticmethod
    def _complete_layout(runtime_root: Path) -> None:
        python_path = runtime_root / ".venv" / "bin" / "python"
        python_path.parent.mkdir(parents=True)
        python_path.write_text("fixture", encoding="utf-8")
        datasets = runtime_root / "cache" / "datasets"
        datasets.mkdir(parents=True)
        for suffix in DATASET_SUFFIXES:
            (datasets / f"fixture{suffix}").write_text("fixture", encoding="utf-8")

    def test_layout_verification_reports_missing_linux_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "--runtime-root",
                    directory,
                    "--layout-only",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "not_ready")
        self.assertIn(".venv/bin/python", report["missing"])
        self.assertIn("cache/datasets/*.umls_2022_ab_cat0129.jsonl", report["missing"])

    def test_full_verification_observes_the_worker_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_root = Path(directory)
            self._complete_layout(runtime_root)
            worker = runtime_root / "fixture_worker.py"
            worker.write_text(
                """
import json
import sys

print(json.dumps({
    "protocol": "scispacy-umls-worker-v1",
    "type": "ready",
    "extractor": {"name": "fixture_umls", "umls_snapshot": "2022AB"},
}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    if request.get("type") == "shutdown":
        print(json.dumps({
            "protocol": "scispacy-umls-worker-v1",
            "type": "shutdown_ack",
        }), flush=True)
        break
""".strip()
                + "\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(VERIFY_SCRIPT),
                    "--runtime-root",
                    str(runtime_root),
                    "--python-path",
                    sys.executable,
                    "--worker",
                    str(worker),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["verification"], "worker_protocol")
        self.assertEqual(report["extractor"]["umls_snapshot"], "2022AB")

    def test_setup_check_validates_cache_without_mutating_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_source = root / "source-cache"
            datasets = cache_source / "datasets"
            datasets.mkdir(parents=True)
            for suffix in DATASET_SUFFIXES:
                (datasets / f"fixture{suffix}").write_text("fixture", encoding="utf-8")
            runtime_root = root / "runtime"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SETUP_SCRIPT),
                    "--runtime-root",
                    str(runtime_root),
                    "--cache-source",
                    str(cache_source),
                    "--check-only",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "source_ready")
        self.assertEqual(report["dataset_count"], 6)
        self.assertFalse(runtime_root.exists())

    def test_runtime_requirements_pin_spacy_cli_dependency(self) -> None:
        requirements = REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines()

        self.assertIn("click==8.5.0", requirements)


if __name__ == "__main__":
    unittest.main()
