from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest

import clinicalnlp_api3.medical_query_resolver as resolver_contract
from clinicalnlp_api3.medical_query_resolver import (
    CandidateEvidence,
    InvalidMedicalQueryDocumentError,
    InvalidQueryResolutionError,
    LOCAL_DICTIONARY_COLLECTIONS,
    LocalDictionaryMatch,
    MedicalQueryDocument,
    MedicalQueryResolver,
    MedicalQuerySegment,
    QUERY_RESOLUTION_MODES,
    QUERY_RESOLUTION_STATUSES,
    QueryResolution,
    QueryResolutionIssue,
    QueryTextSpan,
    ResolvedCandidate,
)


def _raw_evidence(
    *,
    text: str = "기침",
    start_char: int = 0,
) -> CandidateEvidence:
    return CandidateEvidence(
        scope="exact_raw_span",
        raw_span=QueryTextSpan(
            text=text,
            start_char=start_char,
            end_char=start_char + len(text),
        ),
    )


def _translated_evidence(
    *,
    text: str = "cough",
    start_char: int = 9,
) -> CandidateEvidence:
    return CandidateEvidence(
        scope="whole_raw_segment",
        translated_query_span=QueryTextSpan(
            text=text,
            start_char=start_char,
            end_char=start_char + len(text),
        ),
    )


def _dictionary_match(
    *,
    collection: str = "emergency_terms",
    entity_id: str = "emergency:172",
    canonical_ko: str = "기침",
    canonical_en: str | None = "cough",
    retrieval_score: float = 0.93,
) -> LocalDictionaryMatch:
    return LocalDictionaryMatch(
        collection=collection,
        entity_id=entity_id,
        dictionary_version="medical-dictionary-v1",
        canonical_ko=canonical_ko,
        canonical_en=canonical_en,
        retrieval_score=retrieval_score,
    )


def _candidate(
    *,
    route: str = "raw_exact",
    review_status: str = "official",
    segment_id: str = "seg_1",
    evidence: CandidateEvidence | None = None,
    dictionary_match: LocalDictionaryMatch | None = None,
) -> ResolvedCandidate:
    if evidence is None:
        evidence = (
            _translated_evidence()
            if route in {"umls", "ngram_fallback"}
            else _raw_evidence()
        )
    if dictionary_match is None:
        dictionary_match = _dictionary_match()
    return ResolvedCandidate(
        segment_id=segment_id,
        route=route,
        review_status=review_status,
        dictionary_match=dictionary_match,
        evidence=evidence,
    )


class _StaticResolver(MedicalQueryResolver):
    def __init__(self, resolution: object) -> None:
        self.resolution = resolution
        self.received_document: MedicalQueryDocument | None = None

    def _resolve(self, document: MedicalQueryDocument, /) -> QueryResolution:
        self.received_document = document
        return self.resolution


class MedicalQueryResolverContractTests(unittest.TestCase):
    def test_module_exports_the_required_stage_one_contract(self):
        required_names = {
            "QUERY_RESOLUTION_SCHEMA_VERSION",
            "QUERY_RESOLUTION_MODES",
            "QUERY_RESOLUTION_STATUSES",
            "QUERY_ISSUE_LANES",
            "LOCAL_DICTIONARY_COLLECTIONS",
            "QueryResolutionMode",
            "QueryResolutionStatus",
            "QueryRoute",
            "EvidenceScope",
            "QueryIssueLane",
            "LocalDictionaryCollection",
            "InvalidMedicalQueryDocumentError",
            "InvalidQueryResolutionError",
            "MedicalQuerySegment",
            "MedicalQueryDocument",
            "QueryTextSpan",
            "CandidateEvidence",
            "LocalDictionaryMatch",
            "ResolvedCandidate",
            "QueryResolutionIssue",
            "QueryResolution",
            "MedicalQueryResolver",
        }

        self.assertLessEqual(required_names, set(resolver_contract.__all__))

    def test_document_preserves_order_and_rejects_duplicate_ids(self):
        first = MedicalQuerySegment(segment_id="seg_1", raw_text="기침")
        second = MedicalQuerySegment(segment_id="seg_2", raw_text="호흡곤란")
        document = MedicalQueryDocument(segments=(first, second))

        self.assertEqual(
            tuple(segment.segment_id for segment in document.segments),
            ("seg_1", "seg_2"),
        )
        with self.assertRaises(InvalidMedicalQueryDocumentError):
            MedicalQueryDocument(segments=(first, first))

    def test_document_requires_typed_immutable_segments(self):
        segment = MedicalQuerySegment(segment_id="seg_1", raw_text="기침")

        for segments in ([segment], ("seg_1",)):
            with self.subTest(segments=segments):
                with self.assertRaises(InvalidMedicalQueryDocumentError):
                    MedicalQueryDocument(segments=segments)

        self.assertEqual(MedicalQueryDocument(segments=()).segments, ())

    def test_segment_requires_preservable_text_and_string_identity(self):
        invalid_values = (
            {"segment_id": 1, "raw_text": "기침"},
            {"segment_id": "", "raw_text": "기침"},
            {"segment_id": " seg_1", "raw_text": "기침"},
            {"segment_id": "seg_1", "raw_text": ""},
            {"segment_id": "seg_1", "raw_text": None},
            {
                "segment_id": "seg_1",
                "raw_text": "기침",
                "translated_text_en": "   ",
            },
        )

        for values in invalid_values:
            with self.subTest(values=values):
                with self.assertRaises(InvalidMedicalQueryDocumentError):
                    MedicalQuerySegment(**values)

    def test_contract_records_cannot_be_reassigned(self):
        segment = MedicalQuerySegment(segment_id="seg_1", raw_text="기침")
        resolution = QueryResolution(
            mode="shadow",
            status="complete",
            policy_version="medical-query-policy-v1",
        )

        with self.assertRaises(FrozenInstanceError):
            segment.raw_text = "changed"
        with self.assertRaises(FrozenInstanceError):
            resolution.status = "partial"

    def test_span_requires_nonempty_text_and_matching_offsets(self):
        invalid_values = (
            {"text": "기침", "start_char": -1, "end_char": 1},
            {"text": "기침", "start_char": 2, "end_char": 2},
            {"text": "기침", "start_char": 0, "end_char": 3},
            {"text": "", "start_char": 0, "end_char": 1},
            {"text": "기침", "start_char": True, "end_char": 2},
        )

        for values in invalid_values:
            with self.subTest(values=values):
                with self.assertRaises(InvalidQueryResolutionError):
                    QueryTextSpan(**values)

    def test_evidence_keeps_raw_and_translation_provenance_separate(self):
        raw = _raw_evidence()
        translated = _translated_evidence()

        self.assertEqual(raw.raw_span.text, "기침")
        self.assertIsNone(raw.translated_query_span)
        self.assertIsNone(translated.raw_span)
        self.assertEqual(translated.translated_query_span.text, "cough")

    def test_evidence_requires_the_typed_span_for_its_scope(self):
        raw_span = QueryTextSpan(text="기침", start_char=0, end_char=2)
        translated_span = QueryTextSpan(
            text="cough",
            start_char=9,
            end_char=14,
        )
        invalid_values = (
            {"scope": "exact_raw_span"},
            {
                "scope": "exact_raw_span",
                "translated_query_span": translated_span,
            },
            {"scope": "exact_raw_span", "raw_span": "기침"},
            {"scope": "whole_raw_segment"},
            {"scope": "whole_raw_segment", "raw_span": raw_span},
            {
                "scope": "whole_raw_segment",
                "translated_query_span": "cough",
            },
            {"scope": "client_defined", "raw_span": raw_span},
        )

        for values in invalid_values:
            with self.subTest(values=values):
                with self.assertRaises(InvalidQueryResolutionError):
                    CandidateEvidence(**values)

    def test_each_route_has_one_evidence_scope_and_review_policy(self):
        valid_routes = (
            ("raw_exact", "official", _raw_evidence()),
            ("approved_alias", "approved", _raw_evidence()),
            ("raw_similarity", "needs_review", _raw_evidence()),
            ("umls", "needs_review", _translated_evidence()),
            ("ngram_fallback", "needs_review", _translated_evidence()),
        )

        for route, review_status, evidence in valid_routes:
            with self.subTest(route=route):
                candidate = _candidate(
                    route=route,
                    review_status=review_status,
                    evidence=evidence,
                )
                self.assertEqual(candidate.route, route)

                with self.assertRaises(InvalidQueryResolutionError):
                    _candidate(
                        route=route,
                        review_status="approved_by_client",
                        evidence=evidence,
                    )

                wrong_evidence = (
                    _translated_evidence()
                    if evidence.scope == "exact_raw_span"
                    else _raw_evidence()
                )
                with self.assertRaises(InvalidQueryResolutionError):
                    _candidate(
                        route=route,
                        review_status=review_status,
                        evidence=wrong_evidence,
                    )

        downgraded_raw = _candidate(review_status="needs_review")
        self.assertEqual(downgraded_raw.review_status, "needs_review")

    def test_candidate_rejects_unknown_routes_and_missing_evidence(self):
        with self.assertRaises(InvalidQueryResolutionError):
            _candidate(route="client_selected")
        with self.assertRaises(InvalidQueryResolutionError):
            ResolvedCandidate(
                segment_id="seg_1",
                route="raw_exact",
                review_status="official",
                dictionary_match=_dictionary_match(),
            )

    def test_local_dictionary_match_is_immutable_and_bounded(self):
        match = _dictionary_match()

        with self.assertRaises(FrozenInstanceError):
            match.entity_id = "emergency:changed"

        invalid_values = (
            {"dictionary_version": ""},
            {"canonical_ko": ""},
            {"canonical_en": ""},
            {"retrieval_score": -0.01},
            {"retrieval_score": 1.01},
            {"retrieval_score": float("inf")},
        )
        base = {
            "collection": "emergency_terms",
            "entity_id": "emergency:172",
            "dictionary_version": "medical-dictionary-v1",
            "canonical_ko": "기침",
            "canonical_en": "cough",
            "retrieval_score": 0.93,
        }

        for override in invalid_values:
            with self.subTest(override=override):
                with self.assertRaises(InvalidQueryResolutionError):
                    LocalDictionaryMatch(**{**base, **override})

    def test_issue_fields_are_bounded_identifiers(self):
        valid = QueryResolutionIssue(
            code="UMLS_UNAVAILABLE",
            stage="umls_linking",
            lane="umls",
            segment_id="seg_1",
        )
        self.assertEqual(valid.code, "UMLS_UNAVAILABLE")

        invalid_values = (
            {"code": "", "stage": "umls_linking", "lane": "umls"},
            {
                "code": "patient said cough",
                "stage": "umls_linking",
                "lane": "umls",
            },
            {"code": "UMLS_UNAVAILABLE", "stage": "", "lane": "umls"},
            {
                "code": "UMLS_UNAVAILABLE",
                "stage": "patient said cough",
                "lane": "umls",
            },
            {
                "code": "UMLS_UNAVAILABLE",
                "stage": "umls_linking",
                "lane": "client_defined",
            },
            {
                "code": "UMLS_UNAVAILABLE",
                "stage": "umls_linking",
                "lane": "umls",
                "segment_id": 1,
            },
        )

        for values in invalid_values:
            with self.subTest(values=values):
                with self.assertRaises(InvalidQueryResolutionError):
                    QueryResolutionIssue(**values)

    def test_resolution_validates_modes_statuses_and_summary_values(self):
        self.assertEqual(
            QUERY_RESOLUTION_MODES,
            ("legacy", "shadow", "umls_primary"),
        )
        self.assertEqual(QUERY_RESOLUTION_STATUSES, ("complete", "partial"))
        base = {
            "mode": "shadow",
            "status": "complete",
            "policy_version": "medical-query-policy-v1",
        }
        invalid_overrides = (
            {"mode": "client_selected"},
            {"status": "available"},
            {"policy_version": "  "},
            {"umls_query_count": -1},
            {"ngram_query_count": 1.5},
            {"unresolved_count": True},
        )

        for override in invalid_overrides:
            with self.subTest(override=override):
                with self.assertRaises(InvalidQueryResolutionError):
                    QueryResolution(**{**base, **override})

    def test_resolution_requires_typed_immutable_collections(self):
        base = {
            "mode": "shadow",
            "status": "complete",
            "policy_version": "medical-query-policy-v1",
        }
        invalid_overrides = (
            {"candidates": []},
            {"candidates": ({"route": "umls"},)},
            {"issues": []},
            {"issues": ("UMLS_UNAVAILABLE",)},
        )

        for override in invalid_overrides:
            with self.subTest(override=override):
                with self.assertRaises(InvalidQueryResolutionError):
                    QueryResolution(**{**base, **override})

    def test_legacy_and_shadow_keep_umls_as_telemetry_only(self):
        umls_candidate = _candidate(
            route="umls",
            review_status="needs_review",
        )

        shadow_telemetry = QueryResolution(
            mode="shadow",
            status="complete",
            policy_version="medical-query-policy-v1",
            umls_query_count=1,
        )
        self.assertEqual(shadow_telemetry.umls_query_count, 1)

        for mode in ("legacy", "shadow"):
            with self.subTest(mode=mode):
                with self.assertRaises(InvalidQueryResolutionError):
                    QueryResolution(
                        mode=mode,
                        status="complete",
                        policy_version="medical-query-policy-v1",
                        umls_query_count=1,
                        candidates=(umls_candidate,),
                    )

        primary = QueryResolution(
            mode="umls_primary",
            status="complete",
            policy_version="medical-query-policy-v1",
            candidates=(umls_candidate,),
        )
        self.assertEqual(primary.candidates, (umls_candidate,))

        counted = QueryResolution(
            mode="umls_primary",
            status="complete",
            policy_version="medical-query-policy-v1",
            candidates=(_candidate(), _candidate(review_status="needs_review")),
        )
        self.assertEqual(counted.raw_exact_count, 2)

    def test_resolve_accepts_document_traceable_evidence(self):
        translated_text = (
            "There is a possibility of acute angle-closure glaucoma."
        )
        query = "acute angle-closure glaucoma"
        query_start = translated_text.index(query)
        document = MedicalQueryDocument(
            segments=(
                MedicalQuerySegment(
                    segment_id="seg_1",
                    raw_text="기침과 급성 폐쇄각 녹내장 가능성이 있습니다.",
                    translated_text_en=translated_text,
                ),
            )
        )
        expected = QueryResolution(
            mode="umls_primary",
            status="complete",
            policy_version="medical-query-policy-v1",
            candidates=(
                _candidate(),
                _candidate(
                    route="umls",
                    review_status="needs_review",
                    evidence=_translated_evidence(
                        text=query,
                        start_char=query_start,
                    ),
                ),
            ),
        )
        resolver = _StaticResolver(expected)

        self.assertIs(resolver.resolve(document), expected)
        self.assertIs(resolver.received_document, document)

    def test_resolve_rejects_untraceable_candidate_evidence(self):
        document = MedicalQueryDocument(
            segments=(
                MedicalQuerySegment(
                    segment_id="seg_1",
                    raw_text="기침이 있어요.",
                    translated_text_en="I have a cough.",
                ),
            )
        )
        invalid_candidates = (
            _candidate(evidence=_raw_evidence(start_char=1)),
            _candidate(
                route="umls",
                review_status="needs_review",
                evidence=_translated_evidence(start_char=0),
            ),
            _candidate(segment_id="seg_unknown"),
        )

        for candidate in invalid_candidates:
            with self.subTest(candidate=candidate):
                resolution = QueryResolution(
                    mode="umls_primary",
                    status="complete",
                    policy_version="medical-query-policy-v1",
                    candidates=(candidate,),
                )
                with self.assertRaises(InvalidQueryResolutionError):
                    _StaticResolver(resolution).resolve(document)

        untranslated_document = MedicalQueryDocument(
            segments=(MedicalQuerySegment(segment_id="seg_1", raw_text="기침"),)
        )
        translated_resolution = QueryResolution(
            mode="umls_primary",
            status="complete",
            policy_version="medical-query-policy-v1",
            candidates=(
                _candidate(route="umls", review_status="needs_review"),
            ),
        )
        with self.assertRaises(InvalidQueryResolutionError):
            _StaticResolver(translated_resolution).resolve(untranslated_document)

    def test_resolve_rejects_unknown_issue_segments(self):
        document = MedicalQueryDocument(
            segments=(MedicalQuerySegment(segment_id="seg_1", raw_text="기침"),)
        )
        resolution = QueryResolution(
            mode="shadow",
            status="partial",
            policy_version="medical-query-policy-v1",
            issues=(
                QueryResolutionIssue(
                    code="DICTIONARY_UNAVAILABLE",
                    stage="dictionary_search",
                    lane="baseline",
                    segment_id="seg_unknown",
                ),
            ),
        )

        with self.assertRaises(InvalidQueryResolutionError):
            _StaticResolver(resolution).resolve(document)

        known_issue = QueryResolution(
            mode="shadow",
            status="partial",
            policy_version="medical-query-policy-v1",
            issues=(
                QueryResolutionIssue(
                    code="DICTIONARY_UNAVAILABLE",
                    stage="dictionary_search",
                    lane="baseline",
                    segment_id="seg_1",
                ),
            ),
        )
        self.assertIs(
            _StaticResolver(known_issue).resolve(document),
            known_issue,
        )

    def test_resolve_rejects_untyped_input_and_output(self):
        document = MedicalQueryDocument(
            segments=(MedicalQuerySegment(segment_id="seg_1", raw_text="기침"),)
        )
        resolver = _StaticResolver("not-a-resolution")

        with self.assertRaises(InvalidMedicalQueryDocumentError):
            resolver.resolve({"segments": []})
        with self.assertRaises(InvalidQueryResolutionError):
            resolver.resolve(document)


class MedicalQueryResolverFinalSafetyTests(unittest.TestCase):
    def test_candidates_require_a_versioned_local_dictionary_identity(self):
        match = LocalDictionaryMatch(
            collection="emergency_terms",
            entity_id="emergency:172",
            dictionary_version="medical-dictionary-v1",
            canonical_ko="기침",
            canonical_en="cough",
            retrieval_score=0.93,
        )
        candidate = ResolvedCandidate(
            segment_id="seg_1",
            route="umls",
            review_status="needs_review",
            dictionary_match=match,
            evidence=_translated_evidence(),
        )

        self.assertEqual(
            LOCAL_DICTIONARY_COLLECTIONS,
            (
                "drug_terms",
                "procedure_terms",
                "anatomy_terms",
                "emergency_terms",
                "kcd9_terms",
            ),
        )
        self.assertIs(candidate.dictionary_match, match)

        for values in (
            {"collection": "umls", "entity_id": "C0010200"},
            {"collection": "emergency_terms", "entity_id": "C0010200"},
            {"collection": "kcd9_terms", "entity_id": "I10"},
            {"collection": "kcd9_terms", "entity_id": "kcd:"},
        ):
            with self.subTest(values=values):
                with self.assertRaises(InvalidQueryResolutionError):
                    LocalDictionaryMatch(
                        dictionary_version="medical-dictionary-v1",
                        canonical_ko="기침",
                        canonical_en="cough",
                        retrieval_score=0.93,
                        **values,
                    )

    def test_summary_state_and_counts_cannot_contradict_each_other(self):
        issue = QueryResolutionIssue(
            code="DICTIONARY_UNAVAILABLE",
            stage="dictionary_search",
            lane="baseline",
        )

        for values in (
            {"status": "partial", "issues": ()},
            {"status": "complete", "issues": (issue,)},
        ):
            with self.subTest(values=values):
                with self.assertRaises(InvalidQueryResolutionError):
                    QueryResolution(
                        mode="umls_primary",
                        policy_version="medical-query-policy-v1",
                        **values,
                    )

        baseline = {
            "status": "complete",
            "policy_version": "medical-query-policy-v1",
            "ngram_query_count": 2,
        }
        self.assertFalse(QueryResolution(mode="legacy", **baseline).fallback_used)
        self.assertFalse(QueryResolution(mode="shadow", **baseline).fallback_used)
        self.assertTrue(
            QueryResolution(mode="umls_primary", **baseline).fallback_used
        )

    def test_shadow_umls_degradation_is_telemetry_not_public_status(self):
        umls_issue = QueryResolutionIssue(
            code="LINKER_UNAVAILABLE",
            stage="span_linking",
            lane="umls",
        )

        with self.assertRaises(InvalidQueryResolutionError):
            QueryResolution(
                mode="shadow",
                status="partial",
                policy_version="medical-query-policy-v1",
                issues=(umls_issue,),
            )
        with self.assertRaises(InvalidQueryResolutionError):
            QueryResolution(
                mode="legacy",
                status="complete",
                policy_version="medical-query-policy-v1",
                umls_query_count=1,
            )

    def test_public_resolve_wrapper_cannot_be_overridden(self):
        with self.assertRaises(TypeError):

            class UnsafeResolver(MedicalQueryResolver):
                def resolve(self, document):
                    return "unchecked"

                def _resolve(self, document):
                    return "unchecked"

        class UnsafeMixin:
            def resolve(self, document):
                return "unchecked"

        with self.assertRaises(TypeError):

            class UnsafeMroResolver(UnsafeMixin, MedicalQueryResolver):
                def _resolve(self, document):
                    return "unchecked"

    def test_candidate_order_is_validated_at_the_document_boundary(self):
        document = MedicalQueryDocument(
            segments=(
                MedicalQuerySegment(
                    segment_id="seg_1",
                    raw_text="기침이 있어요.",
                    translated_text_en="I have a cough.",
                ),
            )
        )
        out_of_order = QueryResolution(
            mode="umls_primary",
            status="complete",
            policy_version="medical-query-policy-v1",
            candidates=(
                _candidate(
                    route="ngram_fallback",
                    review_status="needs_review",
                ),
                _candidate(),
            ),
        )

        with self.assertRaises(InvalidQueryResolutionError):
            _StaticResolver(out_of_order).resolve(document)

        tied_prefix_out_of_order = QueryResolution(
            mode="umls_primary",
            status="complete",
            policy_version="medical-query-policy-v1",
            candidates=(
                _candidate(evidence=_raw_evidence(text="기침이")),
                _candidate(evidence=_raw_evidence(text="기침")),
            ),
        )
        with self.assertRaises(InvalidQueryResolutionError):
            _StaticResolver(tied_prefix_out_of_order).resolve(document)


if __name__ == "__main__":
    unittest.main()

