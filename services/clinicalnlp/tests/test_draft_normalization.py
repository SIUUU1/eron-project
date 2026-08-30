import unittest

from clinicalnlp_api3.draft_normalization import (
    build_draft_normalization_plan,
    ground_model_draft_suggestions,
)


def _candidate(
    entity_id,
    canonical_en,
    *,
    match_type="umls_dictionary_search",
    retrieval_score=0.9,
    similarity=0.95,
    semantic_types=None,
    collection="emergency_terms",
    entity_type=None,
):
    return {
        "collection": collection,
        "entity_id": entity_id,
        "canonical_ko": "",
        "canonical_en": canonical_en,
        "match_type": match_type,
        "review_status": "needs_review",
        "retrieval_score": retrieval_score,
        "entity_type": entity_type,
        "provenance": {
            "source": "UMLS",
            "similarity": similarity,
            "semantic_types": semantic_types or [],
        },
    }


def _record(value="어큐트 앵글 클로저 글루코마 가능성 있습니다."):
    return {
        "impression": [
            {
                "raw_value": value,
                "status": "needs_confirmation",
                "evidence": {"source_segment_id": "seg_0001"},
            }
        ]
    }


def _segments(candidates, *, source_text=None):
    raw = "어큐트 앵글 클로저 글루코마 가능성 있습니다."
    return [
        {
            "id": "seg_0001",
            "raw_text": raw,
            "annotations": [
                {
                    "source_span": {
                        "text": source_text if source_text is not None else raw,
                        "start_char": 0,
                        "end_char": len(source_text or raw),
                    },
                    "candidates": candidates,
                }
            ],
        }
    ]


class DraftNormalizationTests(unittest.TestCase):
    def test_clear_verified_umls_top_one_builds_a_bounded_model_payload(self):
        direct, payload = build_draft_normalization_plan(
            _record(),
            _segments(
                [
                    _candidate("emergency:1", "Acute angle-closure glaucoma", retrieval_score=0.93),
                    _candidate("emergency:2", "Angle-closure glaucoma", retrieval_score=0.80),
                ]
            ),
        )

        self.assertEqual(direct, [])
        self.assertEqual(len(payload["fields"]), 1)
        field = payload["fields"][0]
        self.assertEqual(field["field_id"], "impression")
        self.assertEqual(len(field["allowed_candidates"]), 1)
        self.assertEqual(
            field["allowed_candidates"][0]["display_value"],
            "Acute angle-closure glaucoma",
        )

    def test_competing_umls_candidates_and_ngram_are_display_only(self):
        _, competing = build_draft_normalization_plan(
            _record(),
            _segments(
                [
                    _candidate("emergency:1", "Acute angle-closure glaucoma", retrieval_score=0.88),
                    _candidate("emergency:2", "Angle-closure glaucoma", retrieval_score=0.82),
                ]
            ),
        )
        _, ngram = build_draft_normalization_plan(
            _record(),
            _segments(
                [
                    _candidate(
                        "emergency:1",
                        "Acute angle-closure glaucoma",
                        match_type="ngram_dictionary_fallback",
                    )
                ]
            ),
        )

        self.assertEqual(competing, {"fields": []})
        self.assertEqual(ngram, {"fields": []})

    def test_approved_exact_alias_is_replaced_without_model(self):
        source = "어큐트 앵글 클로저 글루코마"
        direct, payload = build_draft_normalization_plan(
            _record(),
            _segments(
                [
                    _candidate(
                        "emergency:1",
                        "Acute angle-closure glaucoma",
                        match_type="approved_alias_candidate",
                    )
                ],
                source_text=source,
            ),
        )

        self.assertEqual(payload, {"fields": []})
        self.assertEqual(
            direct[0]["suggested_value"],
            "Acute angle-closure glaucoma 가능성 있습니다.",
        )
        self.assertEqual(direct[0]["applied_candidates"][0]["source"], "RAW_EXACT")

    def test_model_suggestion_must_use_allowed_id_and_preserve_uncertainty(self):
        _, payload = build_draft_normalization_plan(
            _record(),
            _segments([_candidate("emergency:1", "Acute angle-closure glaucoma")]),
        )
        field = payload["fields"][0]
        candidate_id = field["allowed_candidates"][0]["candidate_id"]

        accepted = ground_model_draft_suggestions(
            {
                "draft_suggestions": [
                    {
                        "field_id": "impression",
                        "atom_id": field["atom_id"],
                        "suggested_value": "Acute angle-closure glaucoma 가능성 있습니다.",
                        "applied_candidate_ids": [candidate_id],
                    }
                ]
            },
            payload,
        )
        dropped = ground_model_draft_suggestions(
            {
                "draft_suggestions": [
                    {
                        "field_id": "impression",
                        "atom_id": field["atom_id"],
                        "suggested_value": "Acute angle-closure glaucoma 확진 및 입원 치료",
                        "applied_candidate_ids": [candidate_id],
                    }
                ]
            },
            payload,
        )

        self.assertEqual(len(accepted), 1)
        self.assertEqual(dropped, [])

    def test_incompatible_semantic_type_is_not_offered_to_field_normalization(self):
        medication_record = {
            "medications": {
                "items": [
                    {
                        "raw_value": "폐렴 약을 복용합니다.",
                        "status": "confirmed",
                        "evidence": {"source_segment_id": "seg_0001"},
                    }
                ]
            }
        }
        disease = _candidate(
            "emergency:pneumonia",
            "Pneumonia",
            semantic_types=["T047"],
        )

        direct, payload = build_draft_normalization_plan(
            medication_record,
            _segments([disease]),
        )

        self.assertEqual(direct, [])
        self.assertEqual(payload, {"fields": []})


if __name__ == "__main__":
    unittest.main()

