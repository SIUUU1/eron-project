from __future__ import annotations

import unittest

from clinicalnlp_api3.medical_vector_repository import (
    VectorIdentity,
    VectorIdentityBatch,
)
from scripts.evaluate_vector_fallback import VectorFallbackTrace


class VectorFallbackTraceTests(unittest.TestCase):
    def _batch(self) -> VectorIdentityBatch:
        return VectorIdentityBatch(
            identities=(
                (
                    VectorIdentity("drug_terms", "drug:1", 0.91),
                    VectorIdentity("emergency_terms", "emergency:1", 0.82),
                ),
                (),
            ),
            elapsed_ms=12.5,
            statement_count=3,
            collection_elapsed_ms=(("drug_terms", 10.0),),
        )

    def test_default_trace_redacts_query_text_and_candidate_ids(self) -> None:
        trace = VectorFallbackTrace(full_trace=False)

        trace.record_batch(
            (
                ("amlodipine misspelling", frozenset({
                    "drug_terms",
                    "emergency_terms",
                })),
                ("cough", frozenset({"drug_terms"})),
            ),
            skip_collections_by_index={1: frozenset({"drug_terms"})},
            batch=self._batch(),
        )

        payload = trace.to_dict()
        self.assertEqual(payload["batch_count"], 1)
        self.assertEqual(payload["query_event_count"], 2)
        self.assertEqual(payload["drug_query_count"], 1)
        self.assertEqual(payload["empty_drug_query_count"], 0)
        first, second = payload["events"]
        self.assertNotIn("query_text", first)
        self.assertNotIn("candidate_ids", first)
        self.assertEqual(first["candidate_count"], 2)
        self.assertEqual(
            first["candidate_count_by_collection"],
            {"drug_terms": 1, "emergency_terms": 1},
        )
        self.assertEqual(second["effective_collections"], [])
        self.assertTrue(second["empty"])

    def test_full_trace_requires_explicit_opt_in(self) -> None:
        trace = VectorFallbackTrace(full_trace=True)

        trace.record_batch(
            (("amlodipine misspelling", frozenset({"drug_terms"})),),
            skip_collections_by_index=None,
            batch=VectorIdentityBatch(
                identities=((VectorIdentity("drug_terms", "drug:1", 0.91),),),
                elapsed_ms=4.0,
                statement_count=2,
            ),
        )

        event = trace.to_dict()["events"][0]
        self.assertEqual(event["query_text"], "amlodipine misspelling")
        self.assertEqual(event["candidate_ids"], ["drug_terms:drug:1"])


if __name__ == "__main__":
    unittest.main()
