import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
import json
import threading
from unittest.mock import patch

from clinicalnlp_api3.service import (
    build_service_runtime,
    ConfigurationError,
    ServiceSettings,
    prepare_service,
)


class ClinicalNlpServiceBootstrapTests(unittest.TestCase):
    def test_umls_disabled_runtime_uses_only_official_raw_exact_fallback(self):
        settings = ServiceSettings.from_mapping(
            {
                "OLLAMA_API_KEY": "test-secret",
                "CLINICALNLP_DATABASE_URL": "postgresql://clinical@postgres/eron",
                "CLINICALNLP_UMLS_ENABLED": "false",
            }
        )
        official_fallback = object()

        with patch(
            "clinicalnlp_api3.official_raw_exact.OfficialRawExactRetriever.from_postgres",
            return_value=official_fallback,
        ), patch(
            "clinicalnlp_api3.retrieval.SqliteDictionaryRetriever"
        ) as legacy_lexical:
            bundle = build_service_runtime(settings)

        self.assertIs(bundle.runtime.retriever, official_fallback)
        legacy_lexical.assert_not_called()

    def test_default_internal_http_port_is_8765(self):
        settings = ServiceSettings.from_mapping(
            {
                "OLLAMA_API_KEY": "test-secret",
                "DATABASE_URL": "postgresql+psycopg://user:secret@postgres/eron",
            }
        )

        self.assertEqual(settings.port, 8765)
        self.assertEqual(
            settings.database_url,
            "postgresql+psycopg://user:secret@postgres/eron",
        )

    def test_default_postgres_terminology_requires_database_url(self):
        with self.assertRaises(ConfigurationError) as raised:
            ServiceSettings.from_mapping(
                {
                    "OLLAMA_API_KEY": "test-secret",
                }
            )

        self.assertEqual(
            str(raised.exception),
            "CLINICALNLP_DATABASE_URL is required",
        )

    def test_legacy_sqlite_backend_flags_are_rejected(self):
        with self.assertRaises(ConfigurationError) as raised:
            ServiceSettings.from_mapping(
                {
                    "OLLAMA_API_KEY": "test-secret",
                    "CLINICALNLP_TERMINOLOGY_BACKEND": "shadow",
                    "CLINICALNLP_MEDICAL_VECTOR_BACKEND": "sqlite",
                    "DATABASE_URL": "postgresql+psycopg://user:secret@postgres/eron",
                }
            )

        self.assertEqual(
            str(raised.exception),
            "CLINICALNLP_TERMINOLOGY_BACKEND is no longer supported; "
            "ClinicalNLP storage is PostgreSQL-only",
        )

    def test_missing_dictionary_assets_keep_service_safely_unavailable(self):
        with patch(
            "clinicalnlp_api3.official_raw_exact.OfficialRawExactRetriever.from_postgres",
            side_effect=RuntimeError("missing PG release"),
        ):
            prepared = prepare_service(
                {
                    "CLINICALNLP_HTTP_HOST": "127.0.0.1",
                    "CLINICALNLP_HTTP_PORT": "0",
                    "OLLAMA_API_KEY": "test-secret",
                    "CLINICALNLP_DATABASE_URL": "postgresql://clinical@postgres/eron",
                }
            )
            thread = threading.Thread(
                target=prepared.server.serve_forever,
                daemon=True,
            )
            thread.start()
            try:
                with self.assertRaises(HTTPError) as raised:
                    urlopen(
                        f"http://127.0.0.1:{prepared.server.server_port}/health",
                        timeout=3,
                    )
                status = raised.exception.code
                result = json.loads(raised.exception.read().decode("utf-8"))
            finally:
                prepared.server.shutdown()
                prepared.close()
                thread.join(timeout=3)

        self.assertEqual(status, 503)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "assets")

    def test_missing_key_keeps_health_endpoint_up_in_unavailable_state(self):
        prepared = prepare_service(
            {
                "CLINICALNLP_HTTP_HOST": "127.0.0.1",
                "CLINICALNLP_HTTP_PORT": "0",
                "OLLAMA_API_KEY": "",
            }
        )
        thread = threading.Thread(
            target=prepared.server.serve_forever,
            daemon=True,
        )
        thread.start()
        try:
            with self.assertRaises(HTTPError) as raised:
                urlopen(
                    f"http://127.0.0.1:{prepared.server.server_port}/health",
                    timeout=3,
                )
            status = raised.exception.code
            result = json.loads(raised.exception.read().decode("utf-8"))
        finally:
            prepared.server.shutdown()
            prepared.close()
            thread.join(timeout=3)

        self.assertEqual(status, 503)
        self.assertEqual(
            result,
            {
                "schema_version": "clinicalnlp-health-v1",
                "status": "unavailable",
                "reason": "configuration",
            },
        )

    def test_ollama_cloud_requires_an_api_key_without_echoing_secrets(self):
        with self.assertRaises(ConfigurationError) as raised:
            ServiceSettings.from_mapping(
                {
                    "CLINICAL_LLM_PROVIDER": "ollama_cloud",
                    "OLLAMA_API_KEY": "",
                }
            )

        self.assertEqual(str(raised.exception), "OLLAMA_API_KEY is required")

    def test_runtime_paths_and_limits_are_read_from_the_service_environment(self):
        settings = ServiceSettings.from_mapping(
            {
                "CLINICAL_LLM_PROVIDER": "ollama_cloud",
                "OLLAMA_API_KEY": "test-secret",
                "OLLAMA_BASE_URL": "https://example.invalid/",
                "OLLAMA_MODEL": "gemma4:31b",
                "OLLAMA_TIMEOUT": "170",
                "CLINICALNLP_HTTP_HOST": "127.0.0.1",
                "CLINICALNLP_HTTP_PORT": "8123",
                "CLINICALNLP_HTTP_TIMEOUT": "180",
                "CLINICALNLP_API3_CONTEXT": "8192",
                "CLINICALNLP_GEMMA_MAX_TOKENS": "3072",
                "CLINICALNLP_QUERY_EXPANSION_MAX_TOKENS": "1536",
                "CLINICALNLP_QUERY_EXPANSION_PASSES": "1",
                # Retained deployments may still carry this obsolete setting.
                # Dynamic token budgeting must ignore it.
                "CLINICALNLP_TRANSLATION_BATCH_SIZE": "not-a-number",
                "CLINICALNLP_DATABASE_URL": "postgresql://clinical@postgres/eron",
                "CLINICALNLP_UMLS_ENABLED": "true",
                "CLINICALNLP_UMLS_TIMEOUT": "90",
                "CLINICALNLP_UMLS_PYTHON": "/runtime/scispacy/.venv/bin/python",
                "CLINICALNLP_UMLS_WORKER": "/app/scripts/medical_span_worker.py",
                "CLINICALNLP_UMLS_CACHE_ROOT": "/runtime/scispacy/cache",
            }
        )

        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8123)
        self.assertEqual(settings.request_timeout_seconds, 180.0)
        self.assertEqual(settings.ollama_base_url, "https://example.invalid")
        self.assertEqual(settings.ollama_model, "gemma4:31b")
        self.assertEqual(settings.ollama_timeout_seconds, 170.0)
        self.assertEqual(settings.context_size, 8192)
        self.assertEqual(settings.clinical_max_tokens, 3072)
        self.assertEqual(settings.query_max_tokens, 1536)
        self.assertEqual(settings.query_passes, 1)
        self.assertFalse(hasattr(settings, "translation_batch_size"))
        self.assertEqual(
            settings.database_url,
            "postgresql://clinical@postgres/eron",
        )
        self.assertTrue(settings.umls_enabled)
        self.assertEqual(settings.umls_timeout_seconds, 90.0)
        self.assertEqual(
            settings.umls_python,
            Path("/runtime/scispacy/.venv/bin/python"),
        )
        self.assertEqual(
            settings.umls_worker,
            Path("/app/scripts/medical_span_worker.py"),
        )
        self.assertEqual(
            settings.umls_cache_root,
            Path("/runtime/scispacy/cache"),
        )


if __name__ == "__main__":
    unittest.main()
