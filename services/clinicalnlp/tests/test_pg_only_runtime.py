from __future__ import annotations

from contextlib import contextmanager
import unittest
from unittest.mock import patch

from clinicalnlp_api3.alias_repository import PostgresApprovedAliasStore
from clinicalnlp_api3.medical_vector_repository import (
    UnavailableMedicalVectorRepository,
)
from clinicalnlp_api3.official_raw_exact import OfficialRawExactRetriever
from clinicalnlp_api3.policy_repository import PostgresPolicyEvidenceRepository
from clinicalnlp_api3.service import ServiceSettings, build_service_runtime
from clinicalnlp_api3.terminology_repository import (
    ExactIdentityBatch,
    TerminologyEntity,
)
from clinicalnlp_api3.umls_query_resolver import VerifiedClinicalDictionary


class _PostgresTerminologyFixture:
    version = "postgres:test-release"

    @contextmanager
    def request_session(self):
        yield self

    def lookup(self, collection, entity_id):
        if (collection, entity_id) != ("emergency_terms", "emergency:1"):
            return None
        return TerminologyEntity(
            collection="emergency_terms",
            entity_id="emergency:1",
            canonical_ko="기침",
            canonical_en="cough",
            review_status="official",
        )

    def exact_identities_many(self, requests):
        return ExactIdentityBatch(tuple(() for _ in requests), 1)


class PgOnlyRuntimeTests(unittest.TestCase):
    def test_service_builds_pg_only_runtime_without_discovering_sqlite_assets(self):
        raw_retriever = OfficialRawExactRetriever.from_entries(
            (
                {
                    "term": "기침",
                    "collection": "emergency_terms",
                    "entity_id": "emergency:1",
                    "canonical_ko": "기침",
                    "canonical_en": "cough",
                    "review_status": "official",
                },
            )
        )

        class Worker:
            def start(self):
                return None

            def close(self):
                return None

        settings = ServiceSettings.from_mapping(
            {
                "OLLAMA_API_KEY": "test-secret",
                "CLINICALNLP_DATABASE_URL": "postgresql://clinical@postgres/eron",
                "CLINICALNLP_UMLS_ENABLED": "true",
            }
        )
        with patch(
            "clinicalnlp_api3.official_raw_exact.OfficialRawExactRetriever.from_postgres",
            return_value=raw_retriever,
        ), patch(
            "clinicalnlp_api3.terminology_repository.PostgresTerminologyRepository",
            return_value=_PostgresTerminologyFixture(),
        ), patch(
            "clinicalnlp_api3.medical_vector_repository.PostgresMedicalVectorRepository",
            return_value=UnavailableMedicalVectorRepository(),
        ), patch(
            "clinicalnlp_api3.medical_span_worker.MedicalSpanWorker",
            return_value=Worker(),
        ), patch(
            "clinicalnlp_api3.retrieval.DictionaryPaths.discover",
            side_effect=AssertionError("SQLite dictionary discovery is forbidden"),
        ), patch(
            "sqlite3.connect",
            side_effect=AssertionError("SQLite runtime access is forbidden"),
        ):
            bundle = build_service_runtime(settings)

        try:
            self.assertEqual(bundle.terminology_backend, "postgres")
            self.assertTrue(bundle.vector_enabled)
        finally:
            bundle.close()

    def test_official_raw_exact_uses_loaded_pg_entries_without_sqlite_paths(self):
        retriever = OfficialRawExactRetriever.from_entries(
            (
                {
                    "term": "기침",
                    "collection": "emergency_terms",
                    "entity_id": "emergency:1",
                    "canonical_ko": "기침",
                    "canonical_en": "cough",
                    "review_status": "official",
                },
            )
        )

        matches = retriever.retrieve(raw_text="기침이 심해요", context=[])

        self.assertEqual(
            matches,
            [
                {
                    "source_text": "기침",
                    "start_char": 0,
                    "end_char": 2,
                    "collection": "emergency_terms",
                    "entity_id": "emergency:1",
                    "canonical_ko": "기침",
                    "canonical_en": "cough",
                    "match_type": "official_exact",
                    "review_status": "official",
                    "retrieval_score": 1.0,
                }
            ],
        )

    def test_verified_dictionary_accepts_pg_adapters_without_local_asset_root(self):
        raw_retriever = OfficialRawExactRetriever.from_entries(
            (
                {
                    "term": "기침",
                    "collection": "emergency_terms",
                    "entity_id": "emergency:1",
                    "canonical_ko": "기침",
                    "canonical_en": "cough",
                    "review_status": "official",
                },
            )
        )
        dictionary = VerifiedClinicalDictionary(
            raw_retriever=raw_retriever,
            terminology_repository=_PostgresTerminologyFixture(),
            vector_repository=UnavailableMedicalVectorRepository(),
        )

        hits = dictionary.raw_matches(raw_text="기침이 심해요", context=[])

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].match.entity_id, "emergency:1")
        self.assertEqual(hits[0].span.text, "기침")

    def test_postgres_policy_repository_preserves_policy_result_contract(self):
        rows = [
            (
                "S03-p26-c02",
                "S03",
                "생성형 인공지능 의료기기 허가·심사 가이드라인",
                26,
                "분석적 성능 검증",
                "AI가 최종 확정하지 않고 의료진이 확인해야 한다.",
                "RUNTIME_VALIDATION",
                "OFFICIAL_GUIDELINE",
                0.8,
                0.9,
            ),
        ]

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def execute(self, sql, parameters):
                self.sql = sql
                self.parameters = parameters

            def fetchall(self):
                return rows

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def cursor(self):
                return Cursor()

        with patch(
            "clinicalnlp_api3.policy_repository.psycopg.connect",
            return_value=Connection(),
        ):
            repository = PostgresPolicyEvidenceRepository(
                "postgresql://clinical@postgres/eron"
            )
            result = repository.retrieve(
                "G19",
                "AI 최종 확정 금지",
                usage_scope="RUNTIME_VALIDATION",
                limit=3,
            )

        self.assertEqual(result["rule_id"], "G19")
        self.assertEqual(result["results"][0]["source_id"], "S03")
        self.assertEqual(result["results"][0]["chunk_id"], "S03-p26-c02")
        self.assertEqual(result["results"][0]["page"], 26)
        self.assertGreater(result["results"][0]["retrieval_score"], 0.0)

    def test_postgres_approved_alias_store_returns_versioned_offsets(self):
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def execute(self, sql, parameters=()):
                self.sql = sql

            def fetchone(self):
                return (3,)

            def fetchall(self):
                return [
                    (
                        "candidate-1",
                        "코프",
                        "emergency_terms",
                        "emergency:1",
                        "기침",
                        "cough",
                        "symptom",
                    )
                ]

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def cursor(self):
                return Cursor()

        with patch(
            "clinicalnlp_api3.alias_repository.psycopg.connect",
            return_value=Connection(),
        ):
            store = PostgresApprovedAliasStore(
                "postgresql://clinical@postgres/eron"
            )
            matches = store.find_approved("코프가 심해요")

        self.assertEqual(
            matches,
            [
                {
                    "candidate_id": "candidate-1",
                    "source_alias": "코프",
                    "start_char": 0,
                    "end_char": 2,
                    "collection": "emergency_terms",
                    "entity_id": "emergency:1",
                    "canonical_ko": "기침",
                    "canonical_en": "cough",
                    "entity_type": "symptom",
                    "alias_db_version": 3,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
