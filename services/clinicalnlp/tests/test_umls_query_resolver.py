from __future__ import annotations

from contextlib import closing, nullcontext
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from clinicalnlp_api3.alias_feedback import (
    VersionedAliasStore,
    VersionedApprovedAliasRetriever,
)
from clinicalnlp_api3.medical_query_resolver import (
    LocalDictionaryMatch,
    MedicalQueryDocument,
    MedicalQuerySegment,
)
from clinicalnlp_api3.medical_vector_repository import (
    VectorIdentity,
    VectorIdentityBatch,
)
from clinicalnlp_api3.medical_span_worker import MedicalSpanLinkOutcome
from clinicalnlp_api3.official_raw_exact import OfficialRawExactRetriever
from clinicalnlp_api3.retrieval import SqliteDictionaryRetriever
from clinicalnlp_api3.umls_query_resolver import (
    UmlsPrimaryMedicalQueryResolver,
    VerifiedLocalDictionary,
)
from clinicalnlp_api3.vector_store import build_vector_indexes


def _create_dictionary_fixture(root: Path) -> None:
    with closing(sqlite3.connect(root / "ERON_의약품용어_DB_v1.sqlite")) as db:
        db.executescript(
            """
            CREATE TABLE ingredients(
                ingredient_id INTEGER, canonical_ko TEXT, canonical_en TEXT,
                concept_status TEXT DEFAULT 'OFFICIAL_CODED'
            );
            CREATE TABLE products(
                item_id TEXT, product_name_ko TEXT, product_name_en TEXT
            );
            CREATE TABLE drug_terms(
                term_id INTEGER, entity_type TEXT, entity_id TEXT, term TEXT,
                term_type TEXT, review_status TEXT
            );
            CREATE TABLE stt_aliases(
                alias_id INTEGER, alias TEXT, entity_type TEXT, entity_id TEXT,
                alias_type TEXT, review_status TEXT
            );
            """
        )

    with closing(
        sqlite3.connect(root / "ERON_검사처치시술용어_DB_v1.sqlite")
    ) as db:
        db.executescript(
            """
            CREATE TABLE clinical_terms(
                term_id INTEGER, category TEXT, canonical_name_ko TEXT,
                canonical_name_en TEXT, review_status TEXT
            );
            CREATE TABLE term_aliases(
                alias_id INTEGER, term_id INTEGER, alias TEXT,
                alias_type TEXT, review_status TEXT
            );
            CREATE TABLE term_search(
                term_id INTEGER, name_ko TEXT, name_en TEXT, rank REAL
            );
            """
        )

    with closing(sqlite3.connect(root / "ERON_anatomy_terms.sqlite")) as db:
        db.executescript(
            """
            CREATE TABLE anatomical_terms(
                term_id INTEGER, korean_name TEXT, english_name TEXT,
                latin_name TEXT, verification_status TEXT
            );
            CREATE TABLE anatomical_aliases(
                alias_id INTEGER, term_id INTEGER, alias TEXT
            );
            """
        )

    with closing(
        sqlite3.connect(root / "ERON_응급의학용어_DB_v1.sqlite")
    ) as db:
        db.executescript(
            """
            CREATE TABLE terms(
                term_id INTEGER, standard_ko TEXT, standard_en TEXT,
                review_status TEXT
            );
            CREATE TABLE aliases(
                alias_id INTEGER, term_id INTEGER, alias TEXT,
                alias_type TEXT, review_status TEXT
            );
            CREATE TABLE whisper_errors(
                error_id INTEGER, observed_text TEXT, intended_term_id INTEGER,
                review_status TEXT
            );
            INSERT INTO terms VALUES(1, '기침', 'cough', 'official');
            INSERT INTO aliases VALUES(1, 1, '기침', 'official', 'official');
            INSERT INTO terms VALUES(
                2, '급성 폐쇄각 녹내장',
                'acute angle-closure glaucoma', 'official'
            );
            INSERT INTO aliases VALUES(
                2, 2, 'Acute angle-closure glaucoma', 'english', 'official'
            );
            INSERT INTO aliases VALUES(
                3, 2, 'Angle-Closure Glaucoma', 'english', 'official'
            );
            """
        )

    with closing(sqlite3.connect(root / "hira_kcd9.sqlite")) as db:
        db.executescript(
            """
            CREATE TABLE kcd_codes(
                code TEXT, code_display TEXT, canonical_ko_name TEXT,
                canonical_en_name TEXT, is_complete INTEGER,
                principal_allowed INTEGER, sex_restriction TEXT,
                min_age INTEGER, max_age INTEGER
            );
            CREATE TABLE kcd_terms(
                term_id INTEGER, code TEXT, ko_name TEXT, en_name TEXT,
                is_canonical INTEGER
            );
            CREATE VIRTUAL TABLE kcd_terms_fts USING fts5(
                code UNINDEXED, ko_name, en_name
            );
            """
        )


class _RecordingSpanLinker:
    def __init__(self, spans: list[dict[str, object]]) -> None:
        self.spans = spans
        self.calls: list[tuple[list[dict[str, str]], str]] = []

    def link(self, translated_segments, *, lane):
        self.calls.append((deepcopy(list(translated_segments)), lane))
        return MedicalSpanLinkOutcome(
            status="linked",
            spans=tuple(self.spans),
            extractor=MappingProxyType({"threshold": 0.8}),
            generation=1,
        )


class _FallbackSpanLinker:
    def __init__(self, reason: str = "deadline_exceeded") -> None:
        self.reason = reason
        self.calls: list[list[dict[str, str]]] = []

    def link(self, translated_segments, *, lane):
        self.calls.append(deepcopy(list(translated_segments)))
        return MedicalSpanLinkOutcome(
            status="fallback",
            spans=(),
            extractor=MappingProxyType({}),
            generation=1,
            fallback_reason=self.reason,
        )


class _RecordingDictionary:
    def __init__(self, delegate: VerifiedLocalDictionary) -> None:
        self.delegate = delegate
        self.search_calls: list[tuple[str, int]] = []
        self.exact_search_calls: list[tuple[str, int]] = []
        self.collection_calls: list[frozenset[str] | None] = []
        self.exact_collection_calls: list[frozenset[str] | None] = []

    def raw_matches(self, *, raw_text, context):
        return self.delegate.raw_matches(raw_text=raw_text, context=context)

    def search(self, query_text, *, limit, collections=None):
        self.search_calls.append((query_text, limit))
        self.collection_calls.append(
            frozenset(collections) if collections is not None else None
        )
        return self.delegate.search(
            query_text,
            limit=limit,
            collections=collections,
        )

    def search_exact(self, query_text, *, limit, collections=None):
        self.exact_search_calls.append((query_text, limit))
        self.exact_collection_calls.append(
            frozenset(collections) if collections is not None else None
        )
        return self.delegate.search_exact(
            query_text,
            limit=limit,
            collections=collections,
        )


class _OverReturningDictionary:
    def __init__(self, matches: tuple[LocalDictionaryMatch, ...]) -> None:
        self.matches = matches
        self.search_calls: list[tuple[str, int]] = []

    def raw_matches(self, *, raw_text, context):
        return ()

    def search(self, query_text, *, limit, collections=None):
        del collections
        self.search_calls.append((query_text, limit))
        return self.matches


class _FailingSearchDictionary:
    def __init__(self, delegate: VerifiedLocalDictionary) -> None:
        self.delegate = delegate

    def raw_matches(self, *, raw_text, context):
        return self.delegate.raw_matches(raw_text=raw_text, context=context)

    def search(self, query_text, *, limit, collections=None):
        del collections
        raise sqlite3.OperationalError("patient text must not leak")


class _ScoreByQueryDictionary:
    def __init__(self) -> None:
        self.search_calls: list[str] = []
        self.collection_calls: list[frozenset[str] | None] = []

    def raw_matches(self, *, raw_text, context):
        del raw_text, context
        return ()

    def search(self, query_text, *, limit, collections=None):
        del limit
        self.search_calls.append(query_text)
        self.collection_calls.append(
            frozenset(collections) if collections is not None else None
        )
        score = 0.41 if query_text == "ocular emergency" else 1.0
        return (
            LocalDictionaryMatch(
                collection="emergency_terms",
                entity_id="emergency:2",
                dictionary_version="sha256:" + "a" * 64,
                canonical_ko="급성 폐쇄각 녹내장",
                canonical_en="acute angle-closure glaucoma",
                retrieval_score=score,
            ),
        )


class _SemanticMissFieldHitDictionary:
    def __init__(self) -> None:
        self.collection_calls: list[frozenset[str] | None] = []

    def raw_matches(self, *, raw_text, context):
        del raw_text, context
        return ()

    def search(self, query_text, *, limit, collections=None):
        del query_text, limit
        selected = frozenset(collections) if collections is not None else None
        self.collection_calls.append(selected)
        if selected != frozenset({"drug_terms"}):
            return ()
        return (
            LocalDictionaryMatch(
                collection="drug_terms",
                entity_id="drug:ingredient:10",
                dictionary_version="sha256:" + "b" * 64,
                canonical_ko="암로디핀",
                canonical_en="amlodipine",
                retrieval_score=1.0,
            ),
        )


class _BatchRecordingDictionary:
    def __init__(self) -> None:
        self.calls = []

    def raw_matches(self, *, raw_text, context):
        del raw_text, context
        return ()

    def search_many(
        self,
        requests,
        *,
        limit,
        exact_only=False,
        skip_exact=False,
    ):
        del limit
        requests = tuple(requests)
        self.calls.append((requests, exact_only, skip_exact))
        matches = tuple(() for _ in requests)
        if skip_exact:
            matches = tuple(
                (
                    LocalDictionaryMatch(
                        collection="emergency_terms",
                        entity_id="emergency:2",
                        dictionary_version="sha256:" + "c" * 64,
                        canonical_ko="급성 폐쇄각 녹내장",
                        canonical_en="acute angle-closure glaucoma",
                        retrieval_score=0.91,
                    ),
                )
                for _ in requests
            )
        return SimpleNamespace(
            matches=matches,
            dictionary_ms=0.0,
            vector_ms=0.0,
            exact_statement_count=1 if exact_only else 0,
            vector_statement_count=1 if skip_exact else 0,
            vector_collection_ms=(),
            vector_collection_statement_counts=(),
        )


class OfficialRawExactRetrieverTests(unittest.TestCase):
    def test_matches_only_official_canonical_korean_terms(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _create_dictionary_fixture(root)
            with closing(
                sqlite3.connect(root / "ERON_의약품용어_DB_v1.sqlite")
            ) as db:
                db.execute(
                    "INSERT INTO ingredients VALUES"
                    "(10, '암로디핀', 'amlodipine', 'OFFICIAL_CODED')"
                )
                db.execute(
                    "INSERT INTO ingredients VALUES"
                    "(11, '시험성분', 'test ingredient', 'PENDING')"
                )
                db.execute(
                    "INSERT INTO products VALUES('20', '엠로딘정', 'M-lodipine Tab.')"
                )
                db.commit()
            with closing(
                sqlite3.connect(root / "ERON_검사처치시술용어_DB_v1.sqlite")
            ) as db:
                db.execute(
                    "INSERT INTO clinical_terms VALUES"
                    "(2, '검사', '흉부 CT', 'chest CT', 'official')"
                )
                db.commit()
            with closing(sqlite3.connect(root / "ERON_anatomy_terms.sqlite")) as db:
                db.execute(
                    "INSERT INTO anatomical_terms VALUES"
                    "(3, '심방', 'atrium', 'atrium', 'verified')"
                )
                db.commit()
            with closing(
                sqlite3.connect(root / "ERON_응급의학용어_DB_v1.sqlite")
            ) as db:
                db.execute("INSERT INTO aliases VALUES(4, 1, '숨참', 'colloquial', 'official')")
                db.commit()
            with closing(sqlite3.connect(root / "hira_kcd9.sqlite")) as db:
                db.execute(
                    "INSERT INTO kcd_terms VALUES"
                    "(4, 'I489', '심방세동', 'atrial fibrillation', 1)"
                )
                db.commit()

            retriever = OfficialRawExactRetriever(root)
            raw_text = (
                "기침과 암로디핀 및 시험성분 복용, "
                "흉부 CT에서 심방 확인. 숨참과 심방세동"
            )
            matches = retriever.retrieve(raw_text=raw_text, context=[])

        self.assertEqual(
            {(match["collection"], match["entity_id"]) for match in matches},
            {
                ("emergency_terms", "emergency:1"),
                ("drug_terms", "drug:ingredient:10"),
                ("procedure_terms", "procedure:2"),
                ("anatomy_terms", "anatomy:3"),
            },
        )
        self.assertNotIn("숨참", {match["source_text"] for match in matches})
        self.assertNotIn("심방세동", {match["source_text"] for match in matches})
        self.assertNotIn("시험성분", {match["source_text"] for match in matches})
        self.assertTrue(all(match["match_type"] == "official_exact" for match in matches))
        self.assertTrue(
            all(
                raw_text[match["start_char"] : match["end_char"]]
                == match["source_text"]
                for match in matches
            )
        )


class UmlsPrimaryResolverTests(unittest.TestCase):
    def test_disjoint_field_and_semantic_routes_skip_dictionary_search(self):
        surface = "acute angle-closure glaucoma"
        dictionary = _ScoreByQueryDictionary()
        resolver = UmlsPrimaryMedicalQueryResolver(
            dictionary=dictionary,
            span_linker=_RecordingSpanLinker(
                [{
                    "segment_id": "seg_1",
                    "text": surface,
                    "start_char": 0,
                    "end_char": len(surface),
                    "umls_candidates": [{
                        "cui": "C0154778",
                        "canonical_name": surface,
                        "semantic_types": ["T047"],
                        "linking_score": 0.99,
                    }],
                }]
            ),
        )

        resolution = resolver.resolve(
            MedicalQueryDocument(
                segments=(MedicalQuerySegment(
                    segment_id="seg_1",
                    raw_text="암로디핀을 복용 중입니다.",
                    translated_text_en=surface,
                    collection_hints=frozenset({"drug_terms"}),
                ),)
            )
        )

        self.assertEqual(dictionary.collection_calls, [])
        self.assertEqual(resolution.candidates, ())
        self.assertEqual(resolution.telemetry.routing_conflict_count, 1)
        self.assertEqual(resolution.telemetry.routed_query_count, 0)

    def test_disjoint_field_route_never_falls_back_to_the_unrelated_field_collection(self):
        surface = "acute angle-closure glaucoma"
        dictionary = _SemanticMissFieldHitDictionary()
        resolver = UmlsPrimaryMedicalQueryResolver(
            dictionary=dictionary,
            span_linker=_RecordingSpanLinker(
                [{
                    "segment_id": "seg_1",
                    "text": surface,
                    "start_char": 0,
                    "end_char": len(surface),
                    "umls_candidates": [{
                        "cui": "C0154778",
                        "canonical_name": surface,
                        "semantic_types": ["T047"],
                        "linking_score": 0.99,
                    }],
                }]
            ),
        )

        resolution = resolver.resolve(
            MedicalQueryDocument(
                segments=(MedicalQuerySegment(
                    segment_id="seg_1",
                    raw_text="암로디핀을 복용 중입니다.",
                    translated_text_en=surface,
                    collection_hints=frozenset({"drug_terms"}),
                ),)
            )
        )

        self.assertEqual(dictionary.collection_calls, [])
        self.assertEqual(resolution.candidates, ())
        self.assertEqual(resolution.telemetry.routing_conflict_count, 1)

    def test_vector_search_uses_only_the_umls_canonical_query_after_exact_miss(self):
        surface = "possible angle closure of the eye"
        canonical = "acute angle-closure glaucoma"
        dictionary = _BatchRecordingDictionary()
        resolver = UmlsPrimaryMedicalQueryResolver(
            dictionary=dictionary,
            span_linker=_RecordingSpanLinker(
                [{
                    "segment_id": "seg_1",
                    "text": surface,
                    "surface_query": surface,
                    "start_char": 0,
                    "end_char": len(surface),
                    "umls_candidates": [{
                        "cui": "C0154778",
                        "canonical_name": canonical,
                        "semantic_types": ["T047"],
                        "linking_score": 0.99,
                    }],
                }]
            ),
        )

        resolution = resolver.resolve(
            MedicalQueryDocument(
                segments=(MedicalQuerySegment(
                    segment_id="seg_1",
                    raw_text="급성 폐쇄각 녹내장 가능성이 있습니다.",
                    translated_text_en=surface,
                ),)
            )
        )

        self.assertEqual(
            dictionary.calls,
            [
                (
                    (
                        (surface, frozenset({"emergency_terms"})),
                        (canonical, frozenset({"emergency_terms"})),
                    ),
                    True,
                    False,
                ),
                (
                    ((canonical, frozenset({"emergency_terms"})),),
                    False,
                    True,
                ),
            ],
        )
        self.assertEqual(len(resolution.candidates), 1)

    def test_dictionary_delegates_vector_search_through_repository_seam(self):
        class RecordingVectorRepository:
            version = "test-vector-v1"

            def __init__(self) -> None:
                self.calls = []

            def request_session(self):
                return nullcontext(self)

            def search_many(
                self,
                requests,
                *,
                limit,
                skip_collections_by_index=None,
            ):
                self.calls.append(
                    (requests, limit, skip_collections_by_index)
                )
                return VectorIdentityBatch(
                    identities=((VectorIdentity(
                        "emergency_terms", "emergency:2", 0.91
                    ),),),
                    elapsed_ms=4.25,
                    statement_count=1,
                    collection_elapsed_ms=(("emergency_terms", 4.0),),
                    collection_statement_counts=(("emergency_terms", 1),),
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            vectors = RecordingVectorRepository()
            dictionary = VerifiedLocalDictionary(
                root,
                raw_retriever=OfficialRawExactRetriever(root),
                vector_repository=vectors,
            )
            batch = dictionary.search_many(
                (("possible glaucoma", frozenset({"emergency_terms"})),),
                limit=5,
            )

        self.assertEqual(batch.matches[0][0].entity_id, "emergency:2")
        self.assertEqual(batch.matches[0][0].retrieval_score, 0.91)
        self.assertEqual(batch.vector_ms, 4.25)
        self.assertEqual(batch.vector_statement_count, 1)
        self.assertEqual(
            batch.vector_collection_ms,
            (("emergency_terms", 4.0),),
        )
        self.assertEqual(
            batch.vector_collection_statement_counts,
            (("emergency_terms", 1),),
        )
        self.assertEqual(len(vectors.calls), 1)

    def test_dictionary_batch_reuses_read_only_connections_for_sixty_four_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            dictionary = VerifiedLocalDictionary(
                root,
                raw_retriever=OfficialRawExactRetriever(root),
            )
            real_connect = sqlite3.connect
            with patch(
                "clinicalnlp_api3.umls_query_resolver.sqlite3.connect",
                wraps=real_connect,
            ) as connect:
                with dictionary.request_session():
                    batch = dictionary.search_many(
                        tuple(
                            ("cough" if index == 0 else f"unknown-{index}", None)
                            for index in range(64)
                        ),
                        limit=5,
                    )

        self.assertEqual(len(batch.matches), 64)
        self.assertEqual(batch.matches[0][0].entity_id, "emergency:1")
        self.assertTrue(all(not matches for matches in batch.matches[1:]))
        self.assertLessEqual(connect.call_count, 4)
        self.assertLessEqual(batch.exact_statement_count, 4)
        self.assertEqual(batch.vector_statement_count, 0)

    def test_vector_followup_can_skip_an_already_completed_exact_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            dictionary = VerifiedLocalDictionary(
                root,
                raw_retriever=OfficialRawExactRetriever(root),
            )

            batch = dictionary.search_many(
                (("unknown clinical term", frozenset({"emergency_terms"})),),
                limit=5,
                skip_exact=True,
            )

        self.assertEqual(batch.exact_statement_count, 0)

    def test_sqlite_vec_extension_loads_once_per_request_session(self):
        import sqlite_vec

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            vector_index = root / "medical-vectors.sqlite"
            build_vector_indexes(root, vector_index)
            dictionary = VerifiedLocalDictionary(
                root,
                raw_retriever=OfficialRawExactRetriever(root),
                vector_index=vector_index,
            )
            with patch("sqlite_vec.load", wraps=sqlite_vec.load) as load:
                with dictionary.request_session():
                    dictionary.search_many((("cough", None),), limit=5)
                    dictionary.search_many(
                        (("acute angle closure glaucoma", None),),
                        limit=5,
                    )

        self.assertEqual(load.call_count, 1)

    def test_exact_hit_skips_vector_statements_for_the_selected_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            vector_index = root / "medical-vectors.sqlite"
            build_vector_indexes(root, vector_index)
            dictionary = VerifiedLocalDictionary(
                root,
                raw_retriever=OfficialRawExactRetriever(root),
                vector_index=vector_index,
            )

            batch = dictionary.search_many(
                (("cough", frozenset({"emergency_terms"})),),
                limit=5,
            )
            mixed_batch = dictionary.search_many(
                (
                    (
                        "cough",
                        frozenset({"emergency_terms", "anatomy_terms"}),
                    ),
                ),
                limit=5,
            )

        self.assertEqual(batch.matches[0][0].entity_id, "emergency:1")
        self.assertGreater(batch.exact_statement_count, 0)
        self.assertEqual(batch.vector_statement_count, 0)
        self.assertEqual(mixed_batch.matches[0][0].entity_id, "emergency:1")
        self.assertEqual(mixed_batch.vector_statement_count, 1)

    def test_umls_resolves_before_official_raw_exact_fallback(self):
        events: list[str] = []

        class Linker:
            def link(self, translated_segments, *, lane):
                del translated_segments, lane
                events.append("umls")
                return MedicalSpanLinkOutcome(
                    status="linked",
                    spans=(
                        {
                            "segment_id": "seg_1",
                            "text": "cough",
                            "start_char": 0,
                            "end_char": 5,
                            "umls_candidates": [
                                {
                                    "cui": "C0010200",
                                    "canonical_name": "cough",
                                    "semantic_types": ["T184"],
                                    "linking_score": 0.99,
                                }
                            ],
                        },
                    ),
                    extractor=MappingProxyType({"threshold": 0.8}),
                    generation=1,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _create_dictionary_fixture(root)
            delegate = VerifiedLocalDictionary(
                root,
                raw_retriever=OfficialRawExactRetriever(root),
            )

            class Dictionary:
                def raw_matches(self, *, raw_text, context):
                    events.append("raw_exact")
                    return delegate.raw_matches(raw_text=raw_text, context=context)

                def search(self, query_text, *, limit, collections=None):
                    events.append("dictionary_search")
                    return delegate.search(
                        query_text,
                        limit=limit,
                        collections=collections,
                    )

                def search_exact(self, query_text, *, limit, collections=None):
                    events.append("dictionary_exact")
                    return delegate.search_exact(
                        query_text,
                        limit=limit,
                        collections=collections,
                    )

            resolution = UmlsPrimaryMedicalQueryResolver(
                dictionary=Dictionary(),
                span_linker=Linker(),
            ).resolve(
                MedicalQueryDocument(
                    segments=(
                        MedicalQuerySegment(
                            segment_id="seg_1",
                            raw_text="기침",
                            translated_text_en="cough",
                        ),
                    )
                )
            )

        self.assertEqual(events, ["umls", "dictionary_exact", "raw_exact"])
        self.assertEqual([candidate.route for candidate in resolution.candidates], ["umls"])

    def test_approved_stt_loanword_alias_stays_review_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            with closing(
                sqlite3.connect(root / "ERON_응급의학용어_DB_v1.sqlite")
            ) as db:
                db.execute(
                    "INSERT INTO terms VALUES(30, '호흡곤란', 'dyspnea', 'official')"
                )
                db.commit()
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=VerifiedLocalDictionary(
                    root,
                    raw_retriever=SqliteDictionaryRetriever(root),
                ),
                span_linker=_RecordingSpanLinker([]),
            )

            resolution = resolver.resolve(
                MedicalQueryDocument(
                    segments=(
                        MedicalQuerySegment(
                            segment_id="seg_1",
                            raw_text="디스프니아가 있습니다.",
                        ),
                    )
                )
            )

        self.assertEqual(len(resolution.candidates), 1)
        candidate = resolution.candidates[0]
        self.assertEqual(candidate.route, "approved_alias")
        self.assertEqual(candidate.review_status, "approved")
        self.assertEqual(candidate.evidence.raw_span.text, "디스프니아")

    def test_scoped_umls_miss_retries_unrestricted_exact_without_vector(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            with closing(sqlite3.connect(root / "ERON_anatomy_terms.sqlite")) as db:
                db.execute(
                    "INSERT INTO anatomical_terms VALUES(10, '안와', 'orbit', 'orbita', 'official')"
                )
                db.execute(
                    "INSERT INTO anatomical_aliases VALUES(10, 10, 'orbit')"
                )
                db.commit()
            surface = "orbit"
            dictionary = _RecordingDictionary(
                VerifiedLocalDictionary(
                    root,
                    raw_retriever=SqliteDictionaryRetriever(root),
                )
            )
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=dictionary,
                span_linker=_RecordingSpanLinker(
                    [
                        {
                            "segment_id": "seg_1",
                            "text": surface,
                            "start_char": 0,
                            "end_char": len(surface),
                            "linked": True,
                            "umls_candidates": [
                                {
                                    "cui": "C-WRONG-TYPE",
                                    "canonical_name": surface,
                                    "semantic_types": ["T047"],
                                    "linking_score": 0.99,
                                }
                            ],
                        }
                    ]
                ),
            )

            resolution = resolver.resolve(
                MedicalQueryDocument(
                    segments=(
                        MedicalQuerySegment(
                            segment_id="seg_1",
                            raw_text="눈 주위가 아픕니다.",
                            translated_text_en=surface,
                        ),
                    )
                )
            )

        self.assertEqual(resolution.umls_query_count, 1)
        self.assertEqual(resolution.ngram_query_count, 1)
        self.assertEqual(resolution.unresolved_count, 0)
        self.assertEqual(resolution.policy_version, "umls-primary-policy-v2")
        self.assertEqual(len(resolution.candidates), 1)
        self.assertEqual(resolution.candidates[0].route, "ngram_fallback")
        self.assertEqual(
            resolution.candidates[0].dictionary_match.entity_id,
            "anatomy:10",
        )
        self.assertEqual(
            dictionary.collection_calls,
            [frozenset({"emergency_terms"})],
        )
        self.assertEqual(
            dictionary.exact_collection_calls,
            [frozenset({"emergency_terms"}), None],
        )

    def test_surface_and_canonical_keep_the_strongest_verified_match(self):
        surface = "ocular emergency"
        dictionary = _ScoreByQueryDictionary()
        resolver = UmlsPrimaryMedicalQueryResolver(
            dictionary=dictionary,
            span_linker=_RecordingSpanLinker(
                [
                    {
                        "segment_id": "seg_1",
                        "text": surface,
                        "start_char": 0,
                        "end_char": len(surface),
                        "linked": True,
                        "umls_candidates": [
                            {
                                "cui": "C0154778",
                                "canonical_name": "acute angle-closure glaucoma",
                                "semantic_types": ["T047"],
                                "linking_score": 0.99,
                            }
                        ],
                    }
                ]
            ),
        )

        resolution = resolver.resolve(
            MedicalQueryDocument(
                segments=(
                    MedicalQuerySegment(
                        segment_id="seg_1",
                        raw_text="눈이 아프고 흐립니다.",
                        translated_text_en=surface,
                    ),
                )
            )
        )

        self.assertEqual(
            dictionary.search_calls,
            [surface, "acute angle-closure glaucoma"],
        )
        self.assertEqual(len(resolution.candidates), 1)
        self.assertEqual(
            resolution.candidates[0].dictionary_match.retrieval_score,
            1.0,
        )

    def test_exact_canonical_suppresses_surface_vector_search(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            dictionary = _RecordingDictionary(
                VerifiedLocalDictionary(
                    root,
                    raw_retriever=SqliteDictionaryRetriever(root),
                )
            )
            surface = "ocular emergency"
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=dictionary,
                span_linker=_RecordingSpanLinker(
                    [
                        {
                            "segment_id": "seg_1",
                            "text": surface,
                            "start_char": 0,
                            "end_char": len(surface),
                            "linked": True,
                            "umls_candidates": [
                                {
                                    "cui": "C0154778",
                                    "canonical_name": "acute angle-closure glaucoma",
                                    "semantic_types": ["T047"],
                                    "linking_score": 0.99,
                                }
                            ],
                        }
                    ]
                ),
            )

            resolution = resolver.resolve(
                MedicalQueryDocument(
                    segments=(
                        MedicalQuerySegment(
                            segment_id="seg_1",
                            raw_text="눈이 아프고 흐립니다.",
                            translated_text_en=surface,
                        ),
                    )
                )
            )

        self.assertEqual(dictionary.search_calls, [])
        self.assertEqual(
            [query for query, _ in dictionary.exact_search_calls],
            [surface, "acute angle-closure glaucoma"],
        )
        self.assertEqual(len(resolution.candidates), 1)
        self.assertEqual(
            resolution.candidates[0].dictionary_match.entity_id,
            "emergency:2",
        )

    def test_unknown_umls_semantic_type_uses_unrestricted_exact_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            with closing(sqlite3.connect(root / "ERON_anatomy_terms.sqlite")) as db:
                db.execute(
                    "INSERT INTO anatomical_terms VALUES"
                    "(10, '안와', 'orbit', 'orbita', 'official')"
                )
                db.execute(
                    "INSERT INTO anatomical_aliases VALUES(10, 10, 'orbit')"
                )
                db.commit()
            dictionary = _RecordingDictionary(
                VerifiedLocalDictionary(
                    root,
                    raw_retriever=SqliteDictionaryRetriever(root),
                )
            )
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=dictionary,
                span_linker=_RecordingSpanLinker(
                    [
                        {
                            "segment_id": "seg_1",
                            "text": "orbit",
                            "start_char": 0,
                            "end_char": 5,
                            "linked": True,
                            "umls_candidates": [
                                {
                                    "cui": "C-UNKNOWN-TYPE",
                                    "canonical_name": "orbit",
                                    "semantic_types": ["T999"],
                                    "linking_score": 0.99,
                                }
                            ],
                        }
                    ]
                ),
            )

            resolution = resolver.resolve(
                MedicalQueryDocument(
                    segments=(
                        MedicalQuerySegment(
                            segment_id="seg_1",
                            raw_text="눈 주위가 아픕니다.",
                            translated_text_en="orbit",
                        ),
                    )
                )
            )

        self.assertEqual(dictionary.search_calls, [])
        self.assertEqual(dictionary.exact_collection_calls, [None])
        self.assertEqual(resolution.ngram_query_count, 0)
        self.assertEqual(resolution.candidates[0].route, "umls")
        self.assertEqual(
            resolution.candidates[0].dictionary_match.entity_id,
            "anatomy:10",
        )

    def test_repeated_typed_umls_query_reuses_request_local_search_result(self):
        with tempfile.TemporaryDirectory() as directory:
            dictionary_root = Path(directory)
            _create_dictionary_fixture(dictionary_root)
            dictionary = _RecordingDictionary(
                VerifiedLocalDictionary(
                    dictionary_root,
                    raw_retriever=SqliteDictionaryRetriever(dictionary_root),
                )
            )
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=dictionary,
                span_linker=_RecordingSpanLinker(
                    [
                        {
                            "segment_id": segment_id,
                            "text": "cough",
                            "start_char": 0,
                            "end_char": 5,
                            "linked": True,
                            "umls_candidates": [
                                {
                                    "cui": "C0010200",
                                    "canonical_name": "cough",
                                    "semantic_types": ["T184"],
                                    "linking_score": 0.99,
                                }
                            ],
                        }
                        for segment_id in ("seg_1", "seg_2")
                    ]
                ),
            )

            resolution = resolver.resolve(
                MedicalQueryDocument(
                    segments=tuple(
                        MedicalQuerySegment(
                            segment_id=segment_id,
                            raw_text="기침",
                            translated_text_en="cough",
                        )
                        for segment_id in ("seg_1", "seg_2")
                    )
                )
            )

        self.assertEqual(dictionary.search_calls, [])
        self.assertEqual(dictionary.exact_search_calls, [("cough", 5)])
        self.assertEqual(
            dictionary.exact_collection_calls,
            [frozenset({"emergency_terms"})],
        )
        self.assertEqual(resolution.telemetry.search_cache_hit_count, 1)
        self.assertEqual(len(resolution.candidates), 2)

    def test_unapproved_source_alias_cannot_inherit_official_term_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            with closing(
                sqlite3.connect(root / "ERON_응급의학용어_DB_v1.sqlite")
            ) as db:
                db.execute(
                    "INSERT INTO aliases VALUES(20, 1, '검토중별칭', 'stt', 'pending')"
                )
                db.commit()
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=VerifiedLocalDictionary(
                    root,
                    raw_retriever=SqliteDictionaryRetriever(root),
                ),
                span_linker=_RecordingSpanLinker([]),
            )

            resolution = resolver.resolve(
                MedicalQueryDocument(
                    segments=(
                        MedicalQuerySegment(
                            segment_id="seg_1",
                            raw_text="검토중별칭이 있습니다.",
                        ),
                    )
                )
            )

        self.assertEqual(len(resolution.candidates), 1)
        candidate = resolution.candidates[0]
        self.assertEqual(candidate.route, "raw_exact")
        self.assertEqual(candidate.review_status, "needs_review")

    def test_approved_alias_with_missing_local_target_is_not_exposed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            store = VersionedAliasStore(
                root / "approved-aliases.sqlite",
                confirmation_threshold=2,
            )
            pending = store.submit_selection(
                source_alias="가짜승인별칭",
                collection="emergency_terms",
                entity_id="emergency:999999",
                canonical_ko="가짜 정본",
                canonical_en="phantom",
                entity_type="symptom",
                source_entity_type="symptom",
                actor_ref="clinician-a",
                identity_verified=True,
                direct_entry=False,
            )
            store.confirm_selection(
                pending["candidate_id"],
                actor_ref="clinician-b",
                identity_verified=True,
            )
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=VerifiedLocalDictionary(
                    root,
                    raw_retriever=VersionedApprovedAliasRetriever(
                        SqliteDictionaryRetriever(root),
                        store,
                    ),
                ),
                span_linker=_RecordingSpanLinker([]),
            )

            resolution = resolver.resolve(
                MedicalQueryDocument(
                    segments=(
                        MedicalQuerySegment(
                            segment_id="seg_1",
                            raw_text="가짜승인별칭을 말했습니다.",
                        ),
                    )
                )
            )

        self.assertEqual(resolution.candidates, ())

    def test_umls_identity_wins_over_duplicate_raw_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            dictionary = VerifiedLocalDictionary(
                root,
                raw_retriever=SqliteDictionaryRetriever(root),
            )
            linker = _RecordingSpanLinker(
                [
                    {
                        "segment_id": "seg_1",
                        "text": "Cough",
                        "start_char": 0,
                        "end_char": 5,
                        "linked": True,
                        "umls_candidates": [
                            {
                                "cui": "C0010200",
                                "canonical_name": "Cough",
                                "semantic_types": ["T184"],
                                "linking_score": 0.99,
                            }
                        ],
                    }
                ]
            )
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=dictionary,
                span_linker=linker,
            )

            resolution = resolver.resolve(
                MedicalQueryDocument(
                    segments=(
                        MedicalQuerySegment(
                            segment_id="seg_1",
                            raw_text="기침이 있습니다.",
                            translated_text_en="Cough is present.",
                        ),
                    )
                )
            )

        self.assertEqual(resolution.umls_query_count, 1)
        self.assertEqual(
            [candidate.route for candidate in resolution.candidates],
            ["umls"],
        )

    def test_worker_requests_are_batched_at_fifty_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            dictionary = _RecordingDictionary(
                VerifiedLocalDictionary(
                    root,
                    raw_retriever=SqliteDictionaryRetriever(root),
                )
            )
            linker = _RecordingSpanLinker([])
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=dictionary,
                span_linker=linker,
            )
            document = MedicalQueryDocument(
                segments=tuple(
                    MedicalQuerySegment(
                        segment_id=f"seg_{index:03d}",
                        raw_text=f"원문 {index}",
                        translated_text_en=f"neutralword{index}",
                    )
                    for index in range(51)
                )
            )

            resolver.resolve(document)

        self.assertEqual([len(call[0]) for call in linker.calls], [50, 1])
        self.assertTrue(all(call[1] == "clinical" for call in linker.calls))

    def test_dictionary_search_failure_keeps_raw_candidates_and_is_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            dictionary = _FailingSearchDictionary(
                VerifiedLocalDictionary(
                    root,
                    raw_retriever=SqliteDictionaryRetriever(root),
                )
            )
            surface = "Acute angle-closure glaucoma"
            linker = _RecordingSpanLinker(
                [
                    {
                        "segment_id": "seg_1",
                        "text": surface,
                        "start_char": 0,
                        "end_char": len(surface),
                        "linked": True,
                        "umls_candidates": [
                            {
                                "cui": "C0154778",
                                "canonical_name": "Angle-Closure Glaucoma",
                                "semantic_types": ["T047"],
                                "linking_score": 0.97,
                            }
                        ],
                    }
                ]
            )
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=dictionary,
                span_linker=linker,
            )

            resolution = resolver.resolve(
                MedicalQueryDocument(
                    segments=(
                        MedicalQuerySegment(
                            segment_id="seg_1",
                            raw_text="기침과 급성 폐쇄각 녹내장 가능성이 있습니다.",
                            translated_text_en=surface,
                        ),
                    )
                )
            )

        self.assertEqual(resolution.status, "partial")
        self.assertEqual(
            [(issue.code, issue.stage, issue.lane) for issue in resolution.issues],
            [("DICTIONARY_UNAVAILABLE", "dictionary_search", "baseline")],
        )
        self.assertEqual(len(resolution.candidates), 2)
        self.assertTrue(
            all(candidate.route == "raw_exact" for candidate in resolution.candidates)
        )

    def test_worker_failure_preserves_raw_and_uses_sanitized_ngram_degradation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            with closing(
                sqlite3.connect(root / "ERON_응급의학용어_DB_v1.sqlite")
            ) as db:
                db.executescript(
                    """
                    INSERT INTO terms VALUES(4, '흉통', 'chest pain', 'official');
                    INSERT INTO aliases VALUES(5, 4, 'Chest pain', 'english', 'official');
                    """
                )
            dictionary = VerifiedLocalDictionary(
                root,
                raw_retriever=SqliteDictionaryRetriever(root),
            )
            linker = _FallbackSpanLinker()
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=dictionary,
                span_linker=linker,
            )

            resolution = resolver.resolve(
                MedicalQueryDocument(
                    segments=(
                        MedicalQuerySegment(
                            segment_id="seg_1",
                            raw_text="기침이 있고 가슴이 아픕니다.",
                            translated_text_en="Chest pain is present.",
                        ),
                    )
                )
            )

        self.assertEqual(resolution.status, "partial")
        self.assertEqual(
            [(issue.code, issue.stage, issue.lane) for issue in resolution.issues],
            [("UMLS_UNAVAILABLE", "umls_linking", "umls")],
        )
        self.assertEqual(
            {candidate.route for candidate in resolution.candidates},
            {"raw_exact", "ngram_fallback"},
        )
        self.assertTrue(resolution.fallback_used)
        self.assertNotIn(
            "Chest pain",
            " ".join(issue.code for issue in resolution.issues),
        )

    def test_worker_compaction_offsets_are_projected_to_exact_original_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            dictionary = _RecordingDictionary(
                VerifiedLocalDictionary(
                    root,
                    raw_retriever=SqliteDictionaryRetriever(root),
                )
            )
            original_translation = (
                "Acute   angle-closure\tglaucoma is possible."
            )
            normalized_surface = "Acute angle-closure glaucoma"
            original_surface = "Acute   angle-closure\tglaucoma"
            linker = _RecordingSpanLinker(
                [
                    {
                        "segment_id": "seg_1",
                        "text": normalized_surface,
                        "start_char": 0,
                        "end_char": len(normalized_surface),
                        "linked": True,
                        "umls_candidates": [
                            {
                                "cui": "C0154778",
                                "canonical_name": "Angle-Closure Glaucoma",
                                "semantic_types": ["T047"],
                                "linking_score": 0.97,
                            }
                        ],
                    },
                    {
                        "segment_id": "seg_other",
                        "text": normalized_surface,
                        "start_char": 0,
                        "end_char": len(normalized_surface),
                        "linked": True,
                        "umls_candidates": [],
                    },
                    {
                        "segment_id": "seg_1",
                        "text": "fabricated span",
                        "start_char": 500,
                        "end_char": 515,
                        "linked": True,
                        "umls_candidates": [],
                    },
                ]
            )
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=dictionary,
                span_linker=linker,
            )
            document = MedicalQueryDocument(
                segments=(
                    MedicalQuerySegment(
                        segment_id="seg_1",
                        raw_text="눈이 너무 아프고 앞이 흐립니다.",
                        translated_text_en=original_translation,
                    ),
                )
            )

            resolution = resolver.resolve(document)

        self.assertEqual(
            linker.calls[0][0][0]["translated_text_en"],
            "Acute angle-closure glaucoma is possible.",
        )
        umls = next(
            candidate for candidate in resolution.candidates if candidate.route == "umls"
        )
        self.assertEqual(
            umls.evidence.translated_query_span.text,
            original_surface,
        )
        self.assertEqual(
            original_translation[
                umls.evidence.translated_query_span.start_char :
                umls.evidence.translated_query_span.end_char
            ],
            original_surface,
        )
        self.assertNotIn("fabricated span", [query for query, _ in dictionary.search_calls])

    def test_resolver_enforces_three_candidates_per_medical_span(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            emergency_path = root / "ERON_응급의학용어_DB_v1.sqlite"
            with closing(sqlite3.connect(emergency_path)) as db:
                db.executemany(
                    "INSERT INTO terms VALUES(?, ?, ?, 'official')",
                    [
                        (100 + index, f"검증용어{index}", f"verified term {index}")
                        for index in range(6)
                    ],
                )
                db.commit()
            verified = VerifiedLocalDictionary(
                root,
                raw_retriever=SqliteDictionaryRetriever(root),
            )
            verified_matches = tuple(
                verified.search(f"verified term {index}", limit=1)[0]
                for index in range(6)
            )
            dictionary = _OverReturningDictionary(verified_matches)
            surface = "shared medical concept"
            linker = _RecordingSpanLinker(
                [
                    {
                        "segment_id": "seg_1",
                        "text": surface,
                        "start_char": 0,
                        "end_char": len(surface),
                        "linked": True,
                        "umls_candidates": [
                            {
                                "cui": "C-SHARED",
                                "canonical_name": surface,
                                "semantic_types": ["T047"],
                                "linking_score": 0.99,
                            }
                        ],
                    }
                ]
            )
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=dictionary,
                span_linker=linker,
            )

            resolution = resolver.resolve(
                MedicalQueryDocument(
                    segments=(
                        MedicalQuerySegment(
                            segment_id="seg_1",
                            raw_text="공유 의학 개념입니다.",
                            translated_text_en=surface,
                        ),
                    )
                )
            )

        self.assertEqual(dictionary.search_calls, [(surface, 5)])
        self.assertEqual(len(resolution.candidates), 3)

    def test_query_budget_is_eight_per_segment_and_128_per_document(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            dictionary = _RecordingDictionary(
                VerifiedLocalDictionary(
                    root,
                    raw_retriever=SqliteDictionaryRetriever(root),
                )
            )
            segments = tuple(
                MedicalQuerySegment(
                    segment_id=f"seg_{segment_index:02d}",
                    raw_text=f"원문 {segment_index}",
                    translated_text_en=" ".join(
                        f"token{segment_index}x{token_index}"
                        for token_index in range(10)
                    ),
                )
                for segment_index in range(17)
            )
            linker = _RecordingSpanLinker([])
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=dictionary,
                span_linker=linker,
            )

            resolution = resolver.resolve(MedicalQueryDocument(segments=segments))

        self.assertEqual(resolution.umls_query_count, 0)
        self.assertEqual(resolution.ngram_query_count, 128)
        self.assertEqual(dictionary.search_calls, [])
        self.assertEqual(len(dictionary.exact_search_calls), 128)
        for segment_index in range(16):
            marker = f"token{segment_index}x"
            self.assertEqual(
                sum(marker in query for query, _ in dictionary.exact_search_calls),
                8,
            )
        self.assertFalse(
            any("token16x" in query for query, _ in dictionary.exact_search_calls)
        )
        self.assertTrue(resolution.fallback_used)

    def test_only_highest_scoring_umls_concept_is_used_after_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            dictionary = _RecordingDictionary(
                VerifiedLocalDictionary(
                    root,
                    raw_retriever=SqliteDictionaryRetriever(root),
                )
            )
            translation = "An ocular emergency is possible."
            surface = "ocular emergency"
            start = translation.index(surface)
            linker = _RecordingSpanLinker(
                [
                    {
                        "segment_id": "seg_1",
                        "text": surface,
                        "start_char": start,
                        "end_char": start + len(surface),
                        "linked": True,
                        "umls_candidates": [
                            {
                                "cui": "C001",
                                "canonical_name": "cough",
                                "semantic_types": ["T184"],
                                "linking_score": 0.90,
                            },
                            {
                                "cui": "C002",
                                "canonical_name": "acute angle-closure glaucoma",
                                "semantic_types": ["T047"],
                                "linking_score": 0.96,
                            },
                        ],
                    }
                ]
            )
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=dictionary,
                span_linker=linker,
            )

            resolution = resolver.resolve(
                MedicalQueryDocument(
                    segments=(
                        MedicalQuerySegment(
                            segment_id="seg_1",
                            raw_text="안과 응급질환 가능성이 있습니다.",
                            translated_text_en=translation,
                        ),
                    )
                )
            )

        self.assertEqual(dictionary.search_calls, [])
        self.assertEqual(
            [query for query, _ in dictionary.exact_search_calls],
            [surface, "acute angle-closure glaucoma"],
        )
        self.assertEqual(
            dictionary.exact_collection_calls,
            [frozenset({"emergency_terms"})] * 2,
        )
        self.assertEqual(resolution.umls_query_count, 2)
        self.assertEqual(
            {candidate.dictionary_match.entity_id for candidate in resolution.candidates},
            {"emergency:2"},
        )

    def test_below_threshold_spans_and_uncovered_regions_use_bounded_ngrams(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            with closing(
                sqlite3.connect(root / "ERON_응급의학용어_DB_v1.sqlite")
            ) as db:
                db.executescript(
                    """
                    INSERT INTO terms VALUES(3, '호흡곤란', 'dyspnea', 'official');
                    INSERT INTO aliases VALUES(4, 3, 'Dyspnea', 'english', 'official');
                    INSERT INTO terms VALUES(4, '흉통', 'chest pain', 'official');
                    INSERT INTO aliases VALUES(5, 4, 'chest pain', 'english', 'official');
                    """
                )
            dictionary = _RecordingDictionary(
                VerifiedLocalDictionary(
                    root,
                    raw_retriever=SqliteDictionaryRetriever(root),
                )
            )
            self.assertTrue(dictionary.delegate.search("Dyspnea", limit=5))
            translation = "Dyspnea persists; chest pain worsened."
            low_surface = "persists"
            low_start = translation.index(low_surface)
            linker = _RecordingSpanLinker(
                [
                    {
                        "segment_id": "seg_1",
                        "text": "Dyspnea",
                        "start_char": 0,
                        "end_char": len("Dyspnea"),
                        "linked": True,
                        "umls_candidates": [
                            {
                                "cui": "C0013404",
                                "canonical_name": "Dyspnea",
                                "semantic_types": ["T184"],
                                "linking_score": 0.99,
                            }
                        ],
                    },
                    {
                        "segment_id": "seg_1",
                        "text": low_surface,
                        "start_char": low_start,
                        "end_char": low_start + len(low_surface),
                        "linked": True,
                        "umls_candidates": [
                            {
                                "cui": "CLOW",
                                "canonical_name": "Persistent symptom",
                                "semantic_types": ["T033"],
                                "linking_score": 0.799,
                            }
                        ],
                    },
                ]
            )
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=dictionary,
                span_linker=linker,
            )

            resolution = resolver.resolve(
                MedicalQueryDocument(
                    segments=(
                        MedicalQuerySegment(
                            segment_id="seg_1",
                            raw_text="숨이 계속 차고 가슴이 아픕니다.",
                            translated_text_en=translation,
                        ),
                    )
                )
            )

        vector_queries = [query for query, _ in dictionary.search_calls]
        exact_queries = [query for query, _ in dictionary.exact_search_calls]
        queries = vector_queries + exact_queries
        self.assertNotIn("Persistent symptom", queries)
        self.assertNotIn("Dyspnea persists", queries)
        self.assertIn("persists", exact_queries)
        self.assertIn("chest pain", exact_queries)
        self.assertNotIn(None, dictionary.collection_calls)
        self.assertLessEqual(len(queries), 8)
        self.assertEqual(
            resolution.umls_query_count + resolution.ngram_query_count,
            len(queries),
        )
        self.assertTrue(resolution.fallback_used)
        translated_candidates = [
            candidate
            for candidate in resolution.candidates
            if candidate.route in {"umls", "ngram_fallback"}
        ]
        self.assertTrue(translated_candidates)
        self.assertTrue(
            all(candidate.review_status == "needs_review" for candidate in translated_candidates)
        )
        chest_pain = next(
            candidate
            for candidate in translated_candidates
            if candidate.dictionary_match.entity_id == "emergency:4"
        )
        self.assertEqual(chest_pain.route, "ngram_fallback")
        self.assertEqual(
            chest_pain.evidence.translated_query_span.text,
            "chest pain",
        )

    def test_whole_query_vector_hits_are_reverified_against_source_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            vector_index = root / "medical-vectors.sqlite"
            build_vector_indexes(root, vector_index)
            dictionary = VerifiedLocalDictionary(
                root,
                raw_retriever=SqliteDictionaryRetriever(root),
                vector_index=vector_index,
            )

            matches = dictionary.search(
                "acute angle closure glaucoma",
                limit=5,
            )

            self.assertLessEqual(len(matches), 5)
            self.assertEqual(
                {match.entity_id for match in matches},
                {"emergency:2"},
            )
            glaucoma = next(
                match
                for match in matches
                if match.entity_id == "emergency:2"
            )
            self.assertEqual(glaucoma.canonical_ko, "급성 폐쇄각 녹내장")

            with closing(sqlite3.connect(dictionary.paths.emergency)) as db:
                db.execute("DELETE FROM aliases WHERE term_id=2")
                db.execute("DELETE FROM terms WHERE term_id=2")
                db.commit()
            with self.assertRaises(RuntimeError):
                dictionary.search(
                    "acute angle closure glaucoma",
                    limit=5,
                )

    def test_vector_index_is_disabled_when_its_source_hash_no_longer_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            vector_index = root / "medical-vectors.sqlite"
            build_vector_indexes(root, vector_index)
            with closing(
                sqlite3.connect(root / "ERON_응급의학용어_DB_v1.sqlite")
            ) as db:
                db.execute("DELETE FROM aliases WHERE term_id=2")
                db.execute(
                    """
                    UPDATE terms
                       SET standard_ko='충수염', standard_en='appendicitis'
                     WHERE term_id=2
                    """
                )
                db.commit()
            dictionary = VerifiedLocalDictionary(
                root,
                raw_retriever=SqliteDictionaryRetriever(root),
                vector_index=vector_index,
            )

            matches = dictionary.search(
                "acute angle closure glaucoma",
                limit=5,
            )

        self.assertEqual(matches, ())

    def test_raw_then_umls_surface_and_canonical_are_verified_and_review_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _create_dictionary_fixture(root)
            alias_store = VersionedAliasStore(
                root / "approved-aliases.sqlite",
                confirmation_threshold=2,
            )
            pending = alias_store.submit_selection(
                source_alias="숨막용어",
                collection="emergency_terms",
                entity_id="emergency:1",
                canonical_ko="기침",
                canonical_en="cough",
                entity_type="symptom",
                source_entity_type="symptom",
                actor_ref="clinician-a",
                identity_verified=True,
                direct_entry=False,
            )
            alias_store.confirm_selection(
                pending["candidate_id"],
                actor_ref="clinician-b",
                identity_verified=True,
            )
            raw_retriever = VersionedApprovedAliasRetriever(
                SqliteDictionaryRetriever(root),
                alias_store,
            )
            dictionary = VerifiedLocalDictionary(
                root,
                raw_retriever=raw_retriever,
            )

            translation = "Acute angle-closure glaucoma is possible."
            surface = "Acute angle-closure glaucoma"
            worker_spans = [
                {
                    "segment_id": "seg_1",
                    "text": surface,
                    "start_char": 0,
                    "end_char": len(surface),
                    "linked": True,
                    "umls_candidates": [
                        {
                            "cui": "C0154778",
                            "canonical_name": "Angle-Closure Glaucoma",
                            "semantic_types": ["T047"],
                            "linking_score": 0.97,
                        }
                    ],
                }
            ]
            original_worker_spans = deepcopy(worker_spans)
            linker = _RecordingSpanLinker(worker_spans)
            resolver = UmlsPrimaryMedicalQueryResolver(
                dictionary=dictionary,
                span_linker=linker,
            )
            document = MedicalQueryDocument(
                segments=(
                    MedicalQuerySegment(
                        segment_id="seg_1",
                        raw_text="기침과 숨막용어입니다.",
                        translated_text_en=translation,
                    ),
                )
            )

            resolution = resolver.resolve(document)

        self.assertEqual(resolution.mode, "umls_primary")
        self.assertEqual(resolution.status, "complete")
        self.assertEqual(resolution.umls_query_count, 2)
        self.assertEqual(resolution.ngram_query_count, 0)
        self.assertEqual(
            [candidate.route for candidate in resolution.candidates],
            ["raw_exact", "approved_alias", "umls"],
        )
        raw, approved, umls = resolution.candidates
        self.assertEqual(raw.evidence.raw_span.text, "기침")
        self.assertEqual(approved.evidence.raw_span.text, "숨막용어")
        self.assertEqual(approved.review_status, "approved")
        self.assertEqual(umls.review_status, "needs_review")
        self.assertEqual(
            umls.dictionary_match.entity_id,
            "emergency:2",
        )
        self.assertTrue(umls.dictionary_match.dictionary_version.startswith("sha256:"))
        self.assertEqual(
            umls.evidence.translated_query_span.text,
            surface,
        )
        self.assertEqual(
            linker.calls,
            [
                (
                    [
                        {
                            "segment_id": "seg_1",
                            "translated_text_en": translation,
                        }
                    ],
                    "clinical",
                )
            ],
        )
        self.assertEqual(worker_spans, original_worker_spans)
        self.assertEqual(document.segments[0].raw_text, "기침과 숨막용어입니다.")


if __name__ == "__main__":
    unittest.main()

