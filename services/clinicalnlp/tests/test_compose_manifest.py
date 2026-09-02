import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPOSITORY_ROOT / "docker-compose.yml"


def _service_block(compose: str, service_name: str) -> str:
    pattern = re.compile(
        rf"(?ms)^  {re.escape(service_name)}:\n"
        rf"(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|^volumes:\n)"
    )
    match = pattern.search(compose)
    return match.group(0) if match else ""


class ClinicalNlpComposeManifestTests(unittest.TestCase):
    def test_clinicalnlp_is_an_internal_opt_in_service(self):
        compose = COMPOSE_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
        service = _service_block(compose, "clinicalnlp")

        self.assertIn("    profiles:\n      - clinical\n", service)
        self.assertIn("      context: ./services/clinicalnlp\n", service)
        self.assertIn("      dockerfile: Dockerfile\n", service)
        self.assertIn("    init: true\n", service)
        self.assertIn("      - path: ./services/clinicalnlp/.env\n", service)
        self.assertIn("        required: false\n", service)
        self.assertIn("CLINICALNLP_DATABASE_URL: ${DATABASE_URL}", service)
        self.assertIn("      postgres:\n        condition: service_healthy\n", service)
        self.assertIn('      - "8765"\n', service)
        self.assertNotIn("    ports:\n", service)
        self.assertIn(
            "source: ${CLINICALNLP_RUNTIME_ROOT:-./runtime/clinicalnlp}/scispacy",
            service,
        )
        self.assertIn("target: /runtime/scispacy\n", service)
        self.assertNotIn("CLINICALNLP_STATE_ROOT", service)
        self.assertNotIn("/runtime/state", service)
        self.assertIn("      - eron-network\n", service)

        for base_service in ("postgres", "frontend", "nginx"):
            self.assertNotIn(
                "clinicalnlp",
                _service_block(compose, base_service),
            )

    def test_backend_can_call_clinicalnlp_without_forcing_the_profile(self):
        compose = COMPOSE_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
        backend = _service_block(compose, "backend")

        self.assertIn(
            "RECORD_AI_URL: ${RECORD_AI_URL:-http://clinicalnlp:8765}",
            backend,
        )
        self.assertIn(
            "CLINICAL_RECORD_AI_TIMEOUT_SECONDS: "
            "${CLINICAL_RECORD_AI_TIMEOUT_SECONDS:-620}",
            backend,
        )
        self.assertNotIn("      clinicalnlp:", backend)


if __name__ == "__main__":
    unittest.main()
