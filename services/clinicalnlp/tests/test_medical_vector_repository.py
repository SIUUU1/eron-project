from __future__ import annotations

from contextlib import contextmanager
import threading
import unittest

import numpy as np

from clinicalnlp_api3.medical_vector_repository import (
    PostgresMedicalVectorRepository,
)


class _ConstantEmbedder:
    def embed(self, text: str) -> np.ndarray:
        return np.ones(256, dtype=np.float32)


class _PostgresVectorFixture(PostgresMedicalVectorRepository):
    def __init__(self) -> None:
        self._minimum_similarity = 0.38
        self._local = threading.local()
        self._embedder = _ConstantEmbedder()
        self._active_collections = frozenset(
            {
                "drug_terms",
                "procedure_terms",
                "anatomy_terms",
                "emergency_terms",
            }
        )
        self.version = "postgres-vector:test"

    @contextmanager
    def request_session(self):
        yield self

    def _execute(self, sql, parameters):
        indexes = parameters[0]
        collection = parameters[3]
        if collection == "drug_terms":
            entity_type = parameters[4]
            entity_id = (
                "drug:ingredient:1"
                if entity_type == "ingredient"
                else "drug:product:1"
            )
            return [
                (indexes[0], entity_id, "amlodipine", "amlodipine", 0.9)
            ]
        if collection == "emergency_terms":
            return [(indexes[0], "emergency:1", "cough", "cough", 0.9)]
        return []


class PostgresMedicalVectorTelemetryTests(unittest.TestCase):
    def test_search_reports_time_and_statement_count_by_collection(self):
        repository = _PostgresVectorFixture()

        result = repository.search_many(
            (
                ("cough", frozenset({"emergency_terms"})),
                ("amlodipine", frozenset({"drug_terms"})),
                ("unknown procedure", frozenset({"procedure_terms"})),
            ),
            limit=5,
        )

        self.assertEqual(
            result.collection_statement_counts,
            (("drug_terms", 2), ("procedure_terms", 1), ("emergency_terms", 1)),
        )
        self.assertEqual(
            tuple(collection for collection, _ in result.collection_elapsed_ms),
            ("drug_terms", "procedure_terms", "emergency_terms"),
        )
        self.assertTrue(
            all(elapsed_ms >= 0 for _, elapsed_ms in result.collection_elapsed_ms)
        )
        self.assertEqual(
            result.collection_batch_counts,
            (("drug_terms", 1), ("procedure_terms", 1), ("emergency_terms", 1)),
        )
        self.assertEqual(
            result.collection_query_counts,
            (("drug_terms", 1), ("procedure_terms", 1), ("emergency_terms", 1)),
        )
        self.assertEqual(
            result.collection_candidate_counts,
            (("drug_terms", 2), ("procedure_terms", 0), ("emergency_terms", 1)),
        )
        self.assertEqual(
            result.collection_empty_query_counts,
            (("drug_terms", 0), ("procedure_terms", 1), ("emergency_terms", 0)),
        )
        self.assertEqual(
            tuple(
                (collection, partition)
                for collection, partition, _ in result.partition_elapsed_ms
            ),
            (
                ("drug_terms", "ingredient"),
                ("drug_terms", "product"),
                ("procedure_terms", "all"),
                ("emergency_terms", "all"),
            ),
        )
        self.assertEqual(
            result.partition_result_counts,
            (
                ("drug_terms", "ingredient", 1),
                ("drug_terms", "product", 1),
                ("procedure_terms", "all", 0),
                ("emergency_terms", "all", 1),
            ),
        )


if __name__ == "__main__":
    unittest.main()
