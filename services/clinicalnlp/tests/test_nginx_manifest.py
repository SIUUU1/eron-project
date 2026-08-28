import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
NGINX_PATH = REPOSITORY_ROOT / "nginx" / "conf.d" / "eron.conf"


def _location_block(config: str, declaration: str) -> str:
    pattern = re.compile(
        rf"(?ms)^    location {re.escape(declaration)} \{{\n"
        rf"(?P<body>.*?)(?=^    \}}\n)"
    )
    match = pattern.search(config)
    return match.group(0) if match else ""


class ClinicalDraftNginxManifestTests(unittest.TestCase):
    def test_only_the_draft_route_outlives_the_620_second_backend_deadline(self):
        config = NGINX_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
        draft = _location_block(config, "= /api/clinical-records/draft")
        general_api = _location_block(config, "/api/")

        self.assertIn("proxy_pass         http://eron_backend;", draft)
        self.assertIn("proxy_connect_timeout 5s;", draft)
        self.assertIn("proxy_send_timeout 630s;", draft)
        self.assertIn("proxy_read_timeout 630s;", draft)
        self.assertIn("proxy_read_timeout 30s;", general_api)
        self.assertNotIn("630s", general_api)


if __name__ == "__main__":
    unittest.main()
