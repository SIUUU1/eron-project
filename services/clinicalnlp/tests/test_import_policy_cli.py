from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest


SERVICE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVICE_ROOT / "scripts" / "import_policy_index.py"


class PolicyImportCliTests(unittest.TestCase):
    def test_direct_cli_help_resolves_the_service_package(self):
        result = subprocess.run(
            [sys.executable, "-I", str(SCRIPT_PATH), "--help"],
            cwd=SERVICE_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertIn("--index", result.stdout)


if __name__ == "__main__":
    unittest.main()
