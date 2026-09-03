from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Iterable, Literal, Sequence

from .medical_query_resolver import (
    CandidateEvidence,
    LocalDictionaryMatch,
    MedicalQueryDocument,
    MedicalQueryResolver,
    QueryResolution,
    QueryResolutionIssue,
    QueryResolutionTelemetry,
    QueryTextSpan,
    ResolvedCandidate,
    UmlsCandidateProvenance,
)
from .official_raw_exact import OFFICIAL_REVIEW_STATUSES
from .retrieval import DictionaryPaths
from .terminology_repository import (
    SqliteTerminologyRepository,
    TerminologyEntity,
    TerminologyRepository,
)
from .medical_vector_repository import (
    MedicalVectorRepository,
    SqliteMedicalVectorRepository,
    UnavailableMedicalVectorRepository,
)
from .vector_store import dictionary_source_hashes


UMLS_LINK_THRESHOLD = 0.8
MAX_QUERIES_PER_SEGMENT = 8
MAX_QUERIES_PER_DOCUMENT = 128
MAX_CANDIDATES_PER_QUERY = 5
MAX_WORKER_SEGMENTS = 50
MAX_WORKER_TRANSLATED_CHARS = 20_000
UMLS_PRIMARY_POLICY_VERSION = "umls-primary-policy-v2"

_COLLECTION_ORDER = {
    "drug_terms": 0,
    "procedure_terms": 1,
    "anatomy_terms": 2,
    "emergency_terms": 3,
    "kcd9_terms": 4,
}
_VECTOR_COLLECTIONS = (
    "drug_terms",
    "procedure_terms",
    "anatomy_terms",
    "emergency_terms",
)
_ROUTE_ORDER = {
    "raw_exact": 0,
    "approved_alias": 1,
    "raw_similarity": 2,
    "umls": 3,
    "ngram_fallback": 4,
}
_ENGLISH_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’/-]*")
_NGRAM_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "did", "do", "does", "for", "from", "had", "has", "have", "he",
    "her", "his", "i", "in", "is", "it", "its", "of", "on", "or",
    "she", "that", "the", "their", "they", "this", "to", "was", "were",
    "with", "you", "possible", "possibility",
}
_EMERGENCY_UMLS_TYPES = {
    "T019", "T020", "T033", "T034", "T037", "T046", "T047", "T048",
    "T049", "T184", "T190", "T191", "T201",
}
_ANATOMY_UMLS_TYPES = {
    "T017", "T018", "T021", "T022", "T023", "T024", "T029", "T030",
    "T031",
}
_PROCEDURE_UMLS_TYPES = {"T058", "T059", "T060", "T061", "T063", "T074", "T203"}
_DRUG_UMLS_TYPES = {
    "T103", "T109", "T116", "T121", "T123", "T125", "T126", "T127",
    "T129", "T130", "T131", "T195", "T200",
}


@dataclass(frozen=True, slots=True)
class _VerifiedRawHit:
    route: Literal["raw_exact", "approved_alias"]
    review_status: Literal["official", "needs_review", "approved"]
    span: QueryTextSpan
    match: LocalDictionaryMatch


@dataclass(frozen=True, slots=True)
class _NgramQuery:
    search_text: str
    evidence_span: QueryTextSpan


@dataclass(frozen=True, slots=True)
class _DictionarySearchBatch:
    matches: tuple[tuple[LocalDictionaryMatch, ...], ...]
    dictionary_ms: float
    vector_ms: float
    exact_statement_count: int
    vector_statement_count: int
    vector_collection_ms: tuple[tuple[str, float], ...] = ()
    vector_collection_statement_counts: tuple[tuple[str, int], ...] = ()
    vector_collection_batch_counts: tuple[tuple[str, int], ...] = ()
    vector_collection_query_counts: tuple[tuple[str, int], ...] = ()
    vector_collection_candidate_counts: tuple[tuple[str, int], ...] = ()
    vector_collection_empty_query_counts: tuple[tuple[str, int], ...] = ()
    vector_partition_ms: tuple[tuple[str, str, float], ...] = ()
    vector_partition_result_counts: tuple[tuple[str, str, int], ...] = ()


@dataclass(slots=True)
class _ConnectionSession:
    connections: dict[Path, sqlite3.Connection]
    depth: int = 1


@dataclass(frozen=True, slots=True)
class _TranslatedProjection:
    original: str
    normalized: str
    boundaries: tuple[int, ...]

    @classmethod
    def build(cls, original: str) -> "_TranslatedProjection":
        tokens = list(re.finditer(r"\S+", original))
        if not tokens:
            raise ValueError("translated text must contain a non-whitespace token")
        normalized_characters: list[str] = []
        boundaries: list[int] = [tokens[0].start()]
        for token_index, token in enumerate(tokens):
            if token_index:
                normalized_characters.append(" ")
                boundaries.append(token.start())
            for original_index in range(token.start(), token.end()):
                normalized_characters.append(original[original_index])
                boundaries.append(original_index + 1)
        normalized = "".join(normalized_characters)
        if len(boundaries) != len(normalized) + 1:
            raise RuntimeError("invalid translated projection")
        return cls(
            original=original,
            normalized=normalized,
            boundaries=tuple(boundaries),
        )

    def original_span(
        self,
        *,
        worker_text: object,
        start_char: object,
        end_char: object,
    ) -> QueryTextSpan | None:
        if (
            not isinstance(worker_text, str)
            or not isinstance(start_char, int)
            or isinstance(start_char, bool)
            or not isinstance(end_char, int)
            or isinstance(end_char, bool)
            or start_char < 0
            or end_char <= start_char
            or end_char > len(self.normalized)
            or self.normalized[start_char:end_char] != worker_text
        ):
            return None
        original_start = self.boundaries[start_char]
        original_end = self.boundaries[end_char]
        if original_end <= original_start:
            return None
        return QueryTextSpan(
            text=self.original[original_start:original_end],
            start_char=original_start,
            end_char=original_end,
        )


class _DictionaryAssetsChangedError(RuntimeError):
    pass


def _ngram_queries_for_region(
    text: str,
    start_char: int,
    end_char: int,
) -> tuple[_NgramQuery, ...]:
    tokens = [
        match
        for match in _ENGLISH_TOKEN_RE.finditer(text, start_char, end_char)
        if match.start() >= start_char and match.end() <= end_char
    ]
    queries: list[_NgramQuery] = []
    seen: set[tuple[str, int, int]] = set()
    for width in (4, 3, 2, 1):
        if width > len(tokens):
            continue
        for position in range(0, len(tokens) - width + 1):
            window = tokens[position : position + width]
            folded = [match.group(0).casefold() for match in window]
            if (
                all(token in _NGRAM_STOPWORDS for token in folded)
                or folded[0] in _NGRAM_STOPWORDS
                or folded[-1] in _NGRAM_STOPWORDS
            ):
                continue
            query_start = window[0].start()
            query_end = window[-1].end()
            search_text = " ".join(match.group(0) for match in window)
            key = (search_text.casefold(), query_start, query_end)
            if key in seen:
                continue
            seen.add(key)
            queries.append(
                _NgramQuery(
                    search_text=search_text,
                    evidence_span=QueryTextSpan(
                        text=text[query_start:query_end],
                        start_char=query_start,
                        end_char=query_end,
                    ),
                )
            )
    return tuple(queries)


def _complement_regions(
    text_length: int,
    intervals: Iterable[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if start < 0 or end <= start or end > text_length:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    regions: list[tuple[int, int]] = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            regions.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < text_length:
        regions.append((cursor, text_length))
    return tuple(regions)


def _bounded_dictionary_matches(
    values: Iterable[object],
) -> tuple[LocalDictionaryMatch, ...]:
    deduplicated: dict[tuple[str, str], LocalDictionaryMatch] = {}
    for value in values:
        if not isinstance(value, LocalDictionaryMatch):
            continue
        key = (value.collection, value.entity_id)
        current = deduplicated.get(key)
        if current is None or value.retrieval_score > current.retrieval_score:
            deduplicated[key] = value
    return tuple(
        sorted(
            deduplicated.values(),
            key=lambda match: (
                -match.retrieval_score,
                _COLLECTION_ORDER[match.collection],
                match.entity_id,
                match.canonical_ko,
                match.canonical_en or "",
            ),
        )[:MAX_CANDIDATES_PER_QUERY]
    )


def _worker_batches(
    translated_segments: Sequence[dict[str, str]],
) -> tuple[tuple[tuple[dict[str, str], ...], ...], tuple[str, ...]]:
    batches: list[tuple[dict[str, str], ...]] = []
    oversized_segment_ids: list[str] = []
    current: list[dict[str, str]] = []
    current_characters = 0
    for segment in translated_segments:
        text = segment["translated_text_en"]
        if len(text) > MAX_WORKER_TRANSLATED_CHARS:
            oversized_segment_ids.append(segment["segment_id"])
            continue
        if current and (
            len(current) >= MAX_WORKER_SEGMENTS
            or current_characters + len(text) > MAX_WORKER_TRANSLATED_CHARS
        ):
            batches.append(tuple(current))
            current = []
            current_characters = 0
        current.append(segment)
        current_characters += len(text)
    if current:
        batches.append(tuple(current))
    return tuple(batches), tuple(oversized_segment_ids)


def _collections_for_semantic_types(values: object) -> frozenset[str] | None:
    semantic_types = {
        value for value in values if isinstance(value, str)
    } if isinstance(values, list) else set()
    collections: set[str] = set()
    if semantic_types & _EMERGENCY_UMLS_TYPES:
        collections.add("emergency_terms")
    if semantic_types & _ANATOMY_UMLS_TYPES:
        collections.add("anatomy_terms")
    if semantic_types & _PROCEDURE_UMLS_TYPES:
        collections.add("procedure_terms")
    if semantic_types & _DRUG_UMLS_TYPES:
        collections.add("drug_terms")
    return frozenset(collections) if collections else None


def _field_routed_collections(
    semantic_collections: frozenset[str] | None,
    field_collections: frozenset[str] | None,
) -> tuple[
    frozenset[str] | None,
    frozenset[str] | None,
    bool,
]:
    """Intersect UMLS meaning with the grounded field search lane."""

    if semantic_collections is None:
        # Unknown UMLS types keep the existing exact-only safety path. A field
        # classification alone must not authorize broad vector retrieval.
        return None, None, False
    if not field_collections:
        return semantic_collections, None, False
    compatible = semantic_collections & field_collections
    if compatible:
        ordered = sorted(compatible, key=_COLLECTION_ORDER.__getitem__)
        return frozenset(ordered[:2]), None, False
    return semantic_collections, None, True


class VerifiedClinicalDictionary:
    """Verify raw, exact, and vector identities through repository adapters."""

    def __init__(
        self,
        db_root: Path | None = None,
        *,
        raw_retriever: Any,
        vector_index: Path | None = None,
        minimum_vector_similarity: float = 0.38,
        terminology_repository: TerminologyRepository | None = None,
        vector_repository: MedicalVectorRepository | None = None,
        alias_store: Any | None = None,
    ) -> None:
        if not 0.0 <= minimum_vector_similarity <= 1.0:
            raise ValueError("minimum_vector_similarity must be between zero and one")
        self.paths = (
            DictionaryPaths.discover(Path(db_root))
            if db_root is not None
            else None
        )
        if terminology_repository is None and db_root is None:
            raise ValueError(
                "db_root is required when no terminology repository is provided"
            )
        self._terminology_repository = (
            terminology_repository
            if terminology_repository is not None
            else SqliteTerminologyRepository(db_root)
        )
        repository_source_hashes = getattr(
            self._terminology_repository,
            "local_source_hashes",
            None,
        )
        self._source_hashes = (
            dict(repository_source_hashes)
            if isinstance(repository_source_hashes, dict)
            else (
                dictionary_source_hashes(self.paths)
                if self.paths is not None
                else {}
            )
        )
        self.version = self._terminology_repository.version
        self._raw_retriever = getattr(
            raw_retriever,
            "base_retriever",
            raw_retriever,
        )
        self._alias_store = (
            alias_store
            if alias_store is not None
            else getattr(raw_retriever, "alias_store", None)
        )
        self._connection_local = threading.local()
        self._source_stats = self._current_source_stats()
        self._vector_repository = (
            vector_repository
            if vector_repository is not None
            else (
                SqliteMedicalVectorRepository(
                    Path(vector_index),
                    source_hashes=self._source_hashes,
                    minimum_similarity=minimum_vector_similarity,
                )
                if vector_index is not None
                else UnavailableMedicalVectorRepository()
            )
        )

    @contextmanager
    def request_session(self):
        """Reuse repository handles and one alias snapshot per request/thread."""
        with ExitStack() as stack:
            stack.enter_context(self._terminology_repository.request_session())
            stack.enter_context(self._vector_repository.request_session())
            alias_request_session = getattr(
                self._alias_store,
                "request_session",
                None,
            )
            if callable(alias_request_session):
                stack.enter_context(alias_request_session())
            session = getattr(self._connection_local, "session", None)
            if session is None:
                session = _ConnectionSession(connections={})
                self._connection_local.session = session
            else:
                session.depth += 1
            try:
                yield self
            finally:
                session.depth -= 1
                if session.depth == 0:
                    for connection in session.connections.values():
                        try:
                            connection.close()
                        except sqlite3.Error:
                            pass
                    session.connections.clear()
                    del self._connection_local.session

    def _connection(self, path: Path) -> sqlite3.Connection:
        session = getattr(self._connection_local, "session", None)
        if session is None:
            raise RuntimeError("dictionary access requires a request session")
        resolved = path.resolve()
        connection = session.connections.get(resolved)
        if connection is None:
            connection = sqlite3.connect(
                f"file:{resolved}?mode=ro",
                uri=True,
            )
            connection.row_factory = sqlite3.Row
            session.connections[resolved] = connection
        return connection

    def _read_rows(
        self,
        path: Path,
        query: str,
        parameters: Sequence[Any],
    ) -> list[sqlite3.Row]:
        return list(self._connection(path).execute(query, tuple(parameters)))

    @staticmethod
    def _file_stat(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_size, stat.st_mtime_ns

    def _source_paths(self) -> dict[str, Path]:
        if self.paths is None:
            return {}
        return {
            "drug_terms": self.paths.drug,
            "procedure_terms": self.paths.procedure,
            "anatomy_terms": self.paths.anatomy,
            "emergency_terms": self.paths.emergency,
            "kcd9_terms": self.paths.kcd9,
        }

    def _current_source_stats(self) -> dict[str, tuple[int, int] | None]:
        return {
            collection: self._file_stat(path)
            for collection, path in self._source_paths().items()
        }

    def _assets_are_current(self) -> bool:
        return self._current_source_stats() == self._source_stats

    def _ensure_assets_are_current(self) -> None:
        if not self._assets_are_current():
            raise _DictionaryAssetsChangedError(
                "local dictionary assets changed after adapter initialization"
            )

    def lookup(
        self,
        collection: str,
        entity_id: str,
    ) -> TerminologyEntity | None:
        with self.request_session():
            self._ensure_assets_are_current()
            return self._terminology_repository.lookup(collection, entity_id)

    def _match(
        self,
        collection: str,
        entity_id: str,
        retrieval_score: object,
    ) -> LocalDictionaryMatch | None:
        entity = self.lookup(collection, entity_id)
        if entity is None:
            return None
        if (
            isinstance(retrieval_score, bool)
            or not isinstance(retrieval_score, (int, float))
            or not math.isfinite(retrieval_score)
        ):
            return None
        score = min(1.0, max(0.0, float(retrieval_score)))
        return LocalDictionaryMatch(
            collection=entity.collection,
            entity_id=entity.entity_id,
            dictionary_version=self.version,
            canonical_ko=entity.canonical_ko,
            canonical_en=entity.canonical_en,
            retrieval_score=score,
        )

    def raw_matches(
        self,
        *,
        raw_text: str,
        context: list[dict[str, Any]],
    ) -> tuple[_VerifiedRawHit, ...]:
        with self.request_session():
            return self._raw_matches_in_session(raw_text=raw_text, context=context)

    def _raw_matches_in_session(
        self,
        *,
        raw_text: str,
        context: list[dict[str, Any]],
    ) -> tuple[_VerifiedRawHit, ...]:
        self._ensure_assets_are_current()
        raw_candidates = self._raw_retriever.retrieve(raw_text=raw_text, context=context)
        verified: list[_VerifiedRawHit] = []
        for candidate in raw_candidates:
            if not isinstance(candidate, dict) or candidate.get("match_type") not in {
                "alias_exact",
                "official_exact",
                "stt_alias_exact",
            }:
                continue
            source_text = candidate.get("source_text")
            start = candidate.get("start_char")
            end = candidate.get("end_char")
            if (
                not isinstance(source_text, str)
                or not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or start < 0
                or end <= start
                or end > len(raw_text)
                or raw_text[start:end] != source_text
            ):
                continue
            collection = candidate.get("collection")
            entity_id = candidate.get("entity_id")
            if not isinstance(collection, str) or not isinstance(entity_id, str):
                continue
            match = self._match(
                collection,
                entity_id,
                candidate.get("retrieval_score", 1.0),
            )
            entity = self.lookup(collection, entity_id)
            if match is None or entity is None:
                continue
            verified.append(
                _VerifiedRawHit(
                    route=(
                        "approved_alias"
                        if candidate.get("match_type") == "stt_alias_exact"
                        else "raw_exact"
                    ),
                    review_status=(
                        "approved"
                        if candidate.get("match_type") == "stt_alias_exact"
                        else (
                            "official"
                            if entity.review_status in OFFICIAL_REVIEW_STATUSES
                            and str(
                                candidate.get("review_status") or ""
                            ).casefold() in OFFICIAL_REVIEW_STATUSES
                            else "needs_review"
                        )
                    ),
                    span=QueryTextSpan(
                        text=source_text,
                        start_char=start,
                        end_char=end,
                    ),
                    match=match,
                )
            )

        if self._alias_store is not None:
            try:
                approved_aliases = self._alias_store.find_approved(raw_text)
            except Exception:
                approved_aliases = []
            for alias in approved_aliases:
                if not isinstance(alias, dict):
                    continue
                start = alias.get("start_char")
                end = alias.get("end_char")
                collection = alias.get("collection")
                entity_id = alias.get("entity_id")
                alias_version = alias.get("alias_db_version")
                if (
                    not isinstance(start, int)
                    or isinstance(start, bool)
                    or not isinstance(end, int)
                    or isinstance(end, bool)
                    or start < 0
                    or end <= start
                    or end > len(raw_text)
                    or not isinstance(alias_version, int)
                    or isinstance(alias_version, bool)
                    or alias_version <= 0
                    or not isinstance(collection, str)
                    or not isinstance(entity_id, str)
                ):
                    continue
                source_text = raw_text[start:end]
                if source_text.casefold() != str(alias.get("source_alias") or "").casefold():
                    continue
                match = self._match(collection, entity_id, 1.0)
                if match is None:
                    continue
                verified.append(
                    _VerifiedRawHit(
                        route="approved_alias",
                        review_status="approved",
                        span=QueryTextSpan(
                            text=source_text,
                            start_char=start,
                            end_char=end,
                        ),
                        match=match,
                    )
                )

        deduplicated: dict[tuple[object, ...], _VerifiedRawHit] = {}
        for hit in verified:
            key = (
                hit.route,
                hit.span.start_char,
                hit.span.end_char,
                hit.match.collection,
                hit.match.entity_id,
            )
            deduplicated.setdefault(key, hit)
        return tuple(
            sorted(
                deduplicated.values(),
                key=lambda hit: (
                    _ROUTE_ORDER[hit.route],
                    hit.span.start_char,
                    hit.span.end_char,
                    -hit.match.retrieval_score,
                    hit.match.collection,
                    hit.match.entity_id,
                ),
            )
        )

    def _exact_query_identities(self, query_text: str) -> list[tuple[str, str]]:
        if self.paths is None:
            return []
        queries = (
            (
                "drug_terms",
                self.paths.drug,
                """
                SELECT 'drug:' || lower(entity_type) || ':' || entity_id entity_id
                  FROM drug_terms WHERE lower(trim(term))=lower(trim(?))
                UNION
                SELECT 'drug:ingredient:' || ingredient_id
                  FROM ingredients WHERE lower(trim(canonical_en))=lower(trim(?))
                UNION
                SELECT 'drug:product:' || item_id
                  FROM products WHERE lower(trim(product_name_en))=lower(trim(?))
                """,
                (query_text, query_text, query_text),
            ),
            (
                "procedure_terms",
                self.paths.procedure,
                """
                SELECT 'procedure:' || t.term_id entity_id
                  FROM clinical_terms t
                 WHERE lower(trim(t.canonical_name_en))=lower(trim(?))
                    OR lower(trim(t.canonical_name_ko))=lower(trim(?))
                UNION
                SELECT 'procedure:' || a.term_id
                  FROM term_aliases a
                 WHERE lower(trim(a.alias))=lower(trim(?))
                """,
                (query_text, query_text, query_text),
            ),
            (
                "anatomy_terms",
                self.paths.anatomy,
                """
                SELECT 'anatomy:' || t.term_id entity_id
                  FROM anatomical_terms t
                 WHERE lower(trim(t.english_name))=lower(trim(?))
                    OR lower(trim(t.latin_name))=lower(trim(?))
                    OR lower(trim(t.korean_name))=lower(trim(?))
                UNION
                SELECT 'anatomy:' || a.term_id
                  FROM anatomical_aliases a
                 WHERE lower(trim(a.alias))=lower(trim(?))
                """,
                (query_text, query_text, query_text, query_text),
            ),
            (
                "emergency_terms",
                self.paths.emergency,
                """
                SELECT 'emergency:' || t.term_id entity_id
                  FROM terms t
                 WHERE lower(trim(t.standard_en))=lower(trim(?))
                    OR lower(trim(t.standard_ko))=lower(trim(?))
                UNION
                SELECT 'emergency:' || a.term_id
                  FROM aliases a
                 WHERE lower(trim(a.alias))=lower(trim(?))
                """,
                (query_text, query_text, query_text),
            ),
        )
        identities: list[tuple[str, str]] = []
        for collection, path, sql, parameters in queries:
            try:
                rows = self._read_rows(path, sql, parameters)
            except (OSError, sqlite3.Error):
                continue
            identities.extend(
                (collection, str(row["entity_id"])) for row in rows
            )
        return identities

    @staticmethod
    def _normalize_search_requests(
        requests: Sequence[tuple[str, Iterable[str] | None]],
    ) -> tuple[tuple[str, frozenset[str]], ...]:
        if len(requests) > 64:
            raise ValueError("dictionary batch may contain at most 64 queries")
        supported = frozenset(
            {
                "drug_terms",
                "procedure_terms",
                "anatomy_terms",
                "emergency_terms",
            }
        )
        normalized: list[tuple[str, frozenset[str]]] = []
        for query_text, collections in requests:
            if not isinstance(query_text, str):
                raise TypeError("dictionary query text must be a string")
            selected = (
                supported
                if collections is None
                else frozenset(collections) & supported
            )
            normalized.append((query_text.strip(), selected))
        return tuple(normalized)

    def _exact_query_identities_many(
        self,
        requests: tuple[tuple[str, frozenset[str]], ...],
    ) -> tuple[dict[int, list[tuple[str, str]]], int]:
        batch = self._terminology_repository.exact_identities_many(requests)
        identities = {
            index: [
                (identity.collection, identity.entity_id)
                for identity in batch.identities[index]
            ]
            for index in range(len(requests))
        }
        return identities, batch.statement_count

    def search(
        self,
        query_text: str,
        *,
        limit: int = MAX_CANDIDATES_PER_QUERY,
        collections: Iterable[str] | None = None,
    ) -> tuple[LocalDictionaryMatch, ...]:
        batch = self.search_many(
            ((query_text, collections),),
            limit=limit,
        )
        return batch.matches[0]

    def search_exact(
        self,
        query_text: str,
        *,
        limit: int = MAX_CANDIDATES_PER_QUERY,
        collections: Iterable[str] | None = None,
    ) -> tuple[LocalDictionaryMatch, ...]:
        batch = self.search_many(
            ((query_text, collections),),
            limit=limit,
            exact_only=True,
        )
        return batch.matches[0]

    def search_many(
        self,
        requests: Sequence[tuple[str, Iterable[str] | None]],
        *,
        limit: int = MAX_CANDIDATES_PER_QUERY,
        exact_only: bool = False,
        skip_exact: bool = False,
    ) -> _DictionarySearchBatch:
        if exact_only and skip_exact:
            raise ValueError("exact_only and skip_exact are mutually exclusive")
        bounded_limit = min(MAX_CANDIDATES_PER_QUERY, max(0, int(limit)))
        normalized = self._normalize_search_requests(requests)
        if not normalized:
            return _DictionarySearchBatch((), 0.0, 0.0, 0, 0)
        with self.request_session():
            self._ensure_assets_are_current()
            started = time.perf_counter()
            if skip_exact:
                exact_identities = {
                    index: [] for index in range(len(normalized))
                }
                exact_statements = 0
            else:
                exact_identities, exact_statements = (
                    self._exact_query_identities_many(normalized)
                )
            if exact_only:
                vector_identities = {
                    index: [] for index in range(len(normalized))
                }
                vector_ms = 0.0
                vector_statements = 0
                vector_collection_ms: tuple[tuple[str, float], ...] = ()
                vector_collection_statement_counts: tuple[tuple[str, int], ...] = ()
                vector_collection_batch_counts: tuple[tuple[str, int], ...] = ()
                vector_collection_query_counts: tuple[tuple[str, int], ...] = ()
                vector_collection_candidate_counts: tuple[tuple[str, int], ...] = ()
                vector_collection_empty_query_counts: tuple[tuple[str, int], ...] = ()
                vector_partition_ms: tuple[tuple[str, str, float], ...] = ()
                vector_partition_result_counts: tuple[tuple[str, str, int], ...] = ()
            else:
                (
                    vector_identities,
                    vector_ms,
                    vector_statements,
                    vector_collection_ms,
                    vector_collection_statement_counts,
                    vector_collection_batch_counts,
                    vector_collection_query_counts,
                    vector_collection_candidate_counts,
                    vector_collection_empty_query_counts,
                    vector_partition_ms,
                    vector_partition_result_counts,
                ) = self._vector_query_identities_many(
                    normalized,
                    limit=max(MAX_CANDIDATES_PER_QUERY * 4, bounded_limit * 4),
                    skip_collections_by_index={
                        index: frozenset(
                            collection for collection, _ in identities
                        )
                        for index, identities in exact_identities.items()
                        if identities
                    },
                )
            output: list[tuple[LocalDictionaryMatch, ...]] = []
            for index in range(len(normalized)):
                matches: dict[
                    tuple[str, str], tuple[LocalDictionaryMatch, bool]
                ] = {}
                for collection, entity_id in exact_identities[index]:
                    match = self._match(collection, entity_id, 1.0)
                    if match is not None:
                        matches.setdefault((collection, entity_id), (match, True))
                for collection, entity_id, score in vector_identities[index]:
                    match = self._match(collection, entity_id, score)
                    if match is None:
                        continue
                    key = (collection, entity_id)
                    current = matches.get(key)
                    if current is None or (
                        not current[1]
                        and match.retrieval_score > current[0].retrieval_score
                    ):
                        matches[key] = (match, False)
                output.append(
                    tuple(
                        item[0]
                        for item in sorted(
                            matches.values(),
                            key=lambda item: (
                                -int(item[1]),
                                -item[0].retrieval_score,
                                _COLLECTION_ORDER[item[0].collection],
                                item[0].entity_id,
                            ),
                        )[:bounded_limit]
                    )
                )
            elapsed_ms = (time.perf_counter() - started) * 1000
        return _DictionarySearchBatch(
            matches=tuple(output),
            dictionary_ms=round(max(0.0, elapsed_ms - vector_ms), 3),
            vector_ms=round(vector_ms, 3),
            exact_statement_count=exact_statements,
            vector_statement_count=vector_statements,
            vector_collection_ms=vector_collection_ms,
            vector_collection_statement_counts=vector_collection_statement_counts,
            vector_collection_batch_counts=vector_collection_batch_counts,
            vector_collection_query_counts=vector_collection_query_counts,
            vector_collection_candidate_counts=vector_collection_candidate_counts,
            vector_collection_empty_query_counts=vector_collection_empty_query_counts,
            vector_partition_ms=vector_partition_ms,
            vector_partition_result_counts=vector_partition_result_counts,
        )

    def _vector_query_identities_many(
        self,
        requests: tuple[tuple[str, frozenset[str]], ...],
        *,
        limit: int,
        skip_collections_by_index: dict[int, frozenset[str]] | None = None,
    ) -> tuple[
        dict[int, list[tuple[str, str, float]]],
        float,
        int,
        tuple[tuple[str, float], ...],
        tuple[tuple[str, int], ...],
        tuple[tuple[str, int], ...],
        tuple[tuple[str, int], ...],
        tuple[tuple[str, int], ...],
        tuple[tuple[str, int], ...],
        tuple[tuple[str, str, float], ...],
        tuple[tuple[str, str, int], ...],
    ]:
        batch = self._vector_repository.search_many(
            requests,
            limit=limit,
            skip_collections_by_index=skip_collections_by_index,
        )
        identities = {
            index: [
                (identity.collection, identity.entity_id, identity.similarity)
                for identity in batch.identities[index]
            ]
            for index in range(len(requests))
        }
        return (
            identities,
            batch.elapsed_ms,
            batch.statement_count,
            batch.collection_elapsed_ms,
            batch.collection_statement_counts,
            batch.collection_batch_counts,
            batch.collection_query_counts,
            batch.collection_candidate_counts,
            batch.collection_empty_query_counts,
            batch.partition_elapsed_ms,
            batch.partition_result_counts,
        )


# Backward-compatible name for offline SQLite parity tests and import tools.
VerifiedLocalDictionary = VerifiedClinicalDictionary


class UmlsPrimaryMedicalQueryResolver(MedicalQueryResolver):
    def __init__(
        self,
        *,
        dictionary: VerifiedClinicalDictionary,
        span_linker: Any,
        threshold: float = UMLS_LINK_THRESHOLD,
        policy_version: str = UMLS_PRIMARY_POLICY_VERSION,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between zero and one")
        self._dictionary = dictionary
        self._span_linker = span_linker
        self._threshold = float(threshold)
        self._policy_version = policy_version

    @staticmethod
    def _candidate_sort_key(
        candidate: ResolvedCandidate,
        segment_order: dict[str, int],
    ) -> tuple[object, ...]:
        span = candidate.evidence.raw_span or candidate.evidence.translated_query_span
        assert span is not None
        match = candidate.dictionary_match
        return (
            segment_order[candidate.segment_id],
            _ROUTE_ORDER[candidate.route],
            span.start_char,
            span.end_char,
            span.text,
            -match.retrieval_score,
            match.collection,
            match.entity_id,
            candidate.review_status,
            match.dictionary_version,
            match.canonical_ko,
            match.canonical_en or "",
        )

    def _resolve(self, document: MedicalQueryDocument, /) -> QueryResolution:
        request_session = getattr(self._dictionary, "request_session", None)
        if callable(request_session):
            with request_session():
                return self._resolve_in_session(document)
        return self._resolve_in_session(document)

    def _resolve_in_session(
        self,
        document: MedicalQueryDocument,
        /,
    ) -> QueryResolution:
        candidates: list[ResolvedCandidate] = []
        issues: list[QueryResolutionIssue] = []
        issue_keys: set[tuple[str, str, str, str | None]] = set()
        umls_ms = 0.0
        dictionary_ms = 0.0
        vector_ms = 0.0
        exact_statement_count = 0
        vector_statement_count = 0
        search_cache_hit_count = 0
        routed_query_count = 0
        routing_conflict_count = 0
        exact_search_batch_count = 0
        exact_search_query_count = 0
        exact_search_hit_count = 0
        vector_fallback_batch_count = 0
        vector_fallback_query_count = 0
        vector_fallback_hit_count = 0
        vector_fallback_empty_count = 0
        umls_surface_query_count = 0
        umls_canonical_query_count = 0
        semantic_fallback_query_count = 0
        vector_collection_ms = {
            collection: 0.0 for collection in _VECTOR_COLLECTIONS
        }
        vector_collection_statement_counts = {
            collection: 0 for collection in _VECTOR_COLLECTIONS
        }
        vector_collection_batch_counts = {
            collection: 0 for collection in _VECTOR_COLLECTIONS
        }
        vector_collection_query_counts = {
            collection: 0 for collection in _VECTOR_COLLECTIONS
        }
        vector_collection_candidate_counts = {
            collection: 0 for collection in _VECTOR_COLLECTIONS
        }
        vector_collection_empty_query_counts = {
            collection: 0 for collection in _VECTOR_COLLECTIONS
        }
        vector_partition_ms = {
            ("drug_terms", "ingredient"): 0.0,
            ("drug_terms", "product"): 0.0,
            ("procedure_terms", "all"): 0.0,
            ("anatomy_terms", "all"): 0.0,
            ("emergency_terms", "all"): 0.0,
        }
        vector_partition_result_counts = {
            key: 0 for key in vector_partition_ms
        }
        search_cache: dict[
            tuple[str, frozenset[str] | None, bool, bool],
            tuple[LocalDictionaryMatch, ...],
        ] = {}

        def add_issue(
            code: str,
            stage: str,
            lane: Literal["baseline", "umls"],
            segment_id: str | None = None,
        ) -> None:
            key = (code, stage, lane, segment_id)
            if key in issue_keys:
                return
            issue_keys.add(key)
            issues.append(
                QueryResolutionIssue(
                    code=code,
                    stage=stage,
                    lane=lane,
                    segment_id=segment_id,
                )
            )

        def legacy_search(
            query_text: str,
            allowed_collections: frozenset[str] | None = None,
            *,
            exact_only: bool = False,
        ) -> tuple[LocalDictionaryMatch, ...]:
            nonlocal dictionary_ms
            started = time.perf_counter()
            try:
                search_exact = getattr(self._dictionary, "search_exact", None)
                if exact_only and callable(search_exact):
                    values = search_exact(
                        query_text,
                        limit=MAX_CANDIDATES_PER_QUERY,
                        collections=allowed_collections,
                    )
                else:
                    values = self._dictionary.search(
                        query_text,
                        limit=MAX_CANDIDATES_PER_QUERY,
                        collections=allowed_collections,
                    )
                    if exact_only:
                        values = tuple(
                            match
                            for match in values
                            if isinstance(match, LocalDictionaryMatch)
                            and match.retrieval_score >= 0.999999
                        )
            except Exception:
                add_issue(
                    "DICTIONARY_UNAVAILABLE",
                    "dictionary_search",
                    "baseline",
                )
                return ()
            finally:
                dictionary_ms += (time.perf_counter() - started) * 1000
            return _bounded_dictionary_matches(
                value
                for value in values
                if allowed_collections is None
                or (
                    isinstance(value, LocalDictionaryMatch)
                    and value.collection in allowed_collections
                )
            )

        def safe_search_many(
            requests: Sequence[tuple[str, frozenset[str] | None]],
            *,
            exact_only: bool = False,
            skip_exact: bool = False,
        ) -> tuple[tuple[LocalDictionaryMatch, ...], ...]:
            nonlocal dictionary_ms, vector_ms
            nonlocal exact_statement_count, vector_statement_count
            nonlocal search_cache_hit_count
            nonlocal exact_search_batch_count, exact_search_query_count
            nonlocal exact_search_hit_count
            nonlocal vector_fallback_batch_count, vector_fallback_query_count
            nonlocal vector_fallback_hit_count, vector_fallback_empty_count
            if not requests:
                return ()
            request_keys = tuple(
                (
                    query_text.strip().casefold(),
                    allowed_collections,
                    exact_only,
                    skip_exact,
                )
                for query_text, allowed_collections in requests
            )
            missing_requests: list[tuple[str, frozenset[str] | None]] = []
            missing_keys: list[
                tuple[str, frozenset[str] | None, bool, bool]
            ] = []
            pending_keys: set[
                tuple[str, frozenset[str] | None, bool, bool]
            ] = set()
            for request, key in zip(requests, request_keys):
                if key in search_cache or key in pending_keys:
                    search_cache_hit_count += 1
                    continue
                pending_keys.add(key)
                missing_requests.append(request)
                missing_keys.append(key)
            if not missing_requests:
                return tuple(search_cache[key] for key in request_keys)
            search_many = getattr(self._dictionary, "search_many", None)
            if not callable(search_many):
                missing_values = tuple(
                    legacy_search(
                        query_text,
                        allowed_collections,
                        exact_only=exact_only,
                    )
                    for query_text, allowed_collections in missing_requests
                )
            else:
                try:
                    batch = search_many(
                        missing_requests,
                        limit=MAX_CANDIDATES_PER_QUERY,
                        exact_only=exact_only,
                        skip_exact=skip_exact,
                    )
                except Exception:
                    add_issue(
                        "DICTIONARY_UNAVAILABLE",
                        "dictionary_search",
                        "baseline",
                    )
                    missing_values = tuple(() for _ in missing_requests)
                else:
                    dictionary_ms += float(getattr(batch, "dictionary_ms", 0.0))
                    vector_ms += float(getattr(batch, "vector_ms", 0.0))
                    exact_statement_count += int(
                        getattr(batch, "exact_statement_count", 0)
                    )
                    vector_statement_count += int(
                        getattr(batch, "vector_statement_count", 0)
                    )
                    for collection, elapsed_ms in getattr(
                        batch,
                        "vector_collection_ms",
                        (),
                    ):
                        if collection in vector_collection_ms:
                            vector_collection_ms[collection] += float(elapsed_ms)
                    for collection, statement_count in getattr(
                        batch,
                        "vector_collection_statement_counts",
                        (),
                    ):
                        if collection in vector_collection_statement_counts:
                            vector_collection_statement_counts[collection] += int(
                                statement_count
                            )
                    for attribute, accumulator in (
                        ("vector_collection_batch_counts", vector_collection_batch_counts),
                        ("vector_collection_query_counts", vector_collection_query_counts),
                        (
                            "vector_collection_candidate_counts",
                            vector_collection_candidate_counts,
                        ),
                        (
                            "vector_collection_empty_query_counts",
                            vector_collection_empty_query_counts,
                        ),
                    ):
                        for collection, count in getattr(batch, attribute, ()):
                            if collection in accumulator:
                                accumulator[collection] += int(count)
                    for collection, partition, elapsed_ms in getattr(
                        batch,
                        "vector_partition_ms",
                        (),
                    ):
                        key = (collection, partition)
                        if key in vector_partition_ms:
                            vector_partition_ms[key] += float(elapsed_ms)
                    for collection, partition, count in getattr(
                        batch,
                        "vector_partition_result_counts",
                        (),
                    ):
                        key = (collection, partition)
                        if key in vector_partition_result_counts:
                            vector_partition_result_counts[key] += int(count)
                    values_by_request = getattr(batch, "matches", ())
                    if len(values_by_request) != len(missing_requests):
                        missing_values = tuple(() for _ in missing_requests)
                    else:
                        missing_values = tuple(
                            _bounded_dictionary_matches(
                                value
                                for value in values
                                if allowed_collections is None
                                or (
                                    isinstance(value, LocalDictionaryMatch)
                                    and value.collection in allowed_collections
                                )
                            )
                            for values, (_, allowed_collections) in zip(
                                values_by_request,
                                missing_requests,
                            )
                        )
            if exact_only:
                exact_search_batch_count += 1
                exact_search_query_count += len(missing_values)
                exact_search_hit_count += sum(bool(value) for value in missing_values)
            elif skip_exact:
                vector_fallback_batch_count += 1
                vector_fallback_query_count += len(missing_values)
                hit_count = sum(bool(value) for value in missing_values)
                vector_fallback_hit_count += hit_count
                vector_fallback_empty_count += len(missing_values) - hit_count
            search_cache.update(zip(missing_keys, missing_values))
            return tuple(
                search_cache.get(key, ())
                for key in request_keys
            )

        def safe_search(
            query_text: str,
            allowed_collections: frozenset[str] | None = None,
        ) -> tuple[LocalDictionaryMatch, ...]:
            return safe_search_many(((query_text, allowed_collections),))[0]

        def safe_exact_search(
            query_text: str,
            allowed_collections: frozenset[str] | None = None,
        ) -> tuple[LocalDictionaryMatch, ...]:
            return safe_search_many(
                ((query_text, allowed_collections),),
                exact_only=True,
            )[0]

        context = [
            {"id": segment.segment_id, "text": segment.raw_text}
            for segment in document.segments
        ]
        resolved_identities_by_segment: dict[str, set[tuple[str, str]]] = {
            segment.segment_id: set() for segment in document.segments
        }

        projections = {
            segment.segment_id: _TranslatedProjection.build(
                segment.translated_text_en
            )
            for segment in document.segments
            if segment.translated_text_en is not None
        }
        translated = [
            {
                "segment_id": segment.segment_id,
                "translated_text_en": projections[segment.segment_id].normalized,
            }
            for segment in document.segments
            if segment.segment_id in projections
        ]
        umls_query_count = 0
        ngram_query_count = 0
        unresolved_count = 0
        if translated:
            spans_by_segment: dict[str, list[dict[str, Any]]] = {}
            worker_unavailable = False
            batches, oversized_segment_ids = _worker_batches(translated)
            worker_unavailable = bool(oversized_segment_ids)
            for batch in batches:
                worker_started = time.perf_counter()
                try:
                    outcome = self._span_linker.link(
                        list(batch),
                        lane="clinical",
                    )
                except Exception:
                    outcome = None
                finally:
                    umls_ms += (time.perf_counter() - worker_started) * 1000
                if outcome is None or outcome.fallback_used:
                    worker_unavailable = True
                    continue
                for source_span in outcome.spans:
                    if not isinstance(source_span, dict):
                        continue
                    segment_id = source_span.get("segment_id")
                    projection = projections.get(segment_id)
                    if projection is None:
                        continue
                    original_span = projection.original_span(
                        worker_text=source_span.get("text"),
                        start_char=source_span.get("start_char"),
                        end_char=source_span.get("end_char"),
                    )
                    if original_span is None:
                        continue
                    spans_by_segment.setdefault(segment_id, []).append(
                        {
                            "text": original_span.text,
                            "surface_query": source_span.get("text"),
                            "start_char": original_span.start_char,
                            "end_char": original_span.end_char,
                            "umls_candidates": list(
                                source_span.get("umls_candidates")
                                if isinstance(
                                    source_span.get("umls_candidates"),
                                    list,
                                )
                                else []
                            ),
                        }
                    )
            if worker_unavailable:
                add_issue(
                    "UMLS_UNAVAILABLE",
                    "umls_linking",
                    "umls",
                )

            document_budget = MAX_QUERIES_PER_DOCUMENT
            for segment in document.segments:
                translation = segment.translated_text_en
                if translation is None:
                    continue
                segment_budget = min(MAX_QUERIES_PER_SEGMENT, document_budget)
                worker_intervals: list[tuple[int, int]] = []
                fallback_regions: list[tuple[int, int]] = []
                translated_identities = resolved_identities_by_segment[
                    segment.segment_id
                ]
                for source_span in sorted(
                    spans_by_segment.get(segment.segment_id, []),
                    key=lambda item: (
                        item.get("start_char", -1),
                        item.get("end_char", -1),
                        str(item.get("text") or ""),
                    ),
                ):
                    text = source_span.get("text")
                    start = source_span.get("start_char")
                    end = source_span.get("end_char")
                    if (
                        not isinstance(text, str)
                        or not isinstance(start, int)
                        or isinstance(start, bool)
                        or not isinstance(end, int)
                        or isinstance(end, bool)
                        or start < 0
                        or end <= start
                        or end > len(translation)
                        or translation[start:end] != text
                    ):
                        continue
                    worker_intervals.append((start, end))
                    umls_candidates = source_span.get("umls_candidates")
                    if not isinstance(umls_candidates, list):
                        umls_candidates = []
                    valid_concepts = [
                        concept
                        for concept in umls_candidates
                        if isinstance(concept, dict)
                        and isinstance(concept.get("canonical_name"), str)
                        and concept["canonical_name"].strip()
                        and not isinstance(concept.get("linking_score"), bool)
                        and isinstance(concept.get("linking_score"), (int, float))
                        and math.isfinite(concept["linking_score"])
                        and concept["linking_score"] >= self._threshold
                    ]
                    if not valid_concepts:
                        fallback_regions.append((start, end))
                        continue
                    top_concept = sorted(
                        valid_concepts,
                        key=lambda concept: (
                            -float(concept["linking_score"]),
                            str(concept.get("cui") or ""),
                            str(concept["canonical_name"]).casefold(),
                        ),
                    )[0]
                    concept_cui = str(top_concept.get("cui") or "").strip()
                    concept_semantic_types = tuple(dict.fromkeys(
                        value
                        for value in top_concept.get("semantic_types") or []
                        if isinstance(value, str) and re.fullmatch(r"T\d{3}", value)
                    ))
                    umls_provenance = (
                        UmlsCandidateProvenance(
                            cui=concept_cui,
                            semantic_types=concept_semantic_types,
                            linking_score=float(top_concept["linking_score"]),
                        )
                        if concept_cui
                        else None
                    )
                    semantic_collections = _collections_for_semantic_types(
                        top_concept.get("semantic_types")
                    )
                    (
                        allowed_collections,
                        fallback_collections,
                        routing_conflict,
                    ) = (
                        _field_routed_collections(
                            semantic_collections,
                            segment.collection_hints,
                        )
                    )
                    if routing_conflict:
                        routing_conflict_count += 1
                        # A semantic type and the conversation-grounded draft
                        # field must agree before dictionary/vector retrieval.
                        # Falling back to the field collection here can turn a
                        # generic adjective such as "yellow" into an unrelated
                        # medication candidate.
                        continue
                    query_variants: list[tuple[str, str]] = []
                    surface_query = str(
                        source_span.get("surface_query") or ""
                    ).strip()
                    for query_kind, value in (
                        ("surface", surface_query),
                        (
                            "canonical",
                            str(top_concept["canonical_name"]).strip(),
                        ),
                    ):
                        if not value:
                            continue
                        if value.casefold() not in {
                            query.casefold() for _, query in query_variants
                        }:
                            query_variants.append((query_kind, value))

                    verified_matches: dict[
                        tuple[str, str], LocalDictionaryMatch
                    ] = {}
                    search_requests: list[
                        tuple[str, frozenset[str] | None]
                    ] = []
                    for query_kind, query in query_variants:
                        if segment_budget <= 0 or document_budget <= 0:
                            break
                        segment_budget -= 1
                        document_budget -= 1
                        umls_query_count += 1
                        if segment.collection_hints is not None:
                            routed_query_count += 1
                        if query_kind == "surface":
                            umls_surface_query_count += 1
                        else:
                            umls_canonical_query_count += 1
                        search_requests.append((query, allowed_collections))
                    # Unknown semantic types must not fan out into every vector
                    # collection. Exact lookup remains a bounded safety net,
                    # while typed concepts search only their compatible lanes.
                    exact_matches = safe_search_many(
                        search_requests,
                        exact_only=True,
                    )
                    matches_by_query = (
                        exact_matches
                        if allowed_collections is None
                        or any(exact_matches)
                        else safe_search_many(
                            search_requests[-1:],
                            skip_exact=True,
                        )
                    )
                    if (
                        fallback_collections is not None
                        and not any(matches_by_query)
                    ):
                        fallback_requests = tuple(
                            (query, fallback_collections)
                            for _, query in query_variants[:len(search_requests)]
                        )
                        semantic_fallback_query_count += len(fallback_requests)
                        fallback_exact_matches = safe_search_many(
                            fallback_requests,
                            exact_only=True,
                        )
                        matches_by_query = (
                            fallback_exact_matches
                            if any(fallback_exact_matches)
                            else safe_search_many(
                                fallback_requests[-1:],
                                skip_exact=True,
                            )
                        )
                    for matches in matches_by_query:
                        for match in matches:
                            identity = (match.collection, match.entity_id)
                            current = verified_matches.get(identity)
                            if (
                                current is None
                                or match.retrieval_score
                                > current.retrieval_score
                            ):
                                verified_matches[identity] = match
                    if not verified_matches:
                        fallback_regions.append((start, end))
                        continue
                    evidence = CandidateEvidence(
                        scope="whole_raw_segment",
                        translated_query_span=QueryTextSpan(
                            text=text,
                            start_char=start,
                            end_char=end,
                        ),
                    )
                    ranked_matches = sorted(
                        verified_matches.values(),
                        key=lambda match: (
                            -match.retrieval_score,
                            _COLLECTION_ORDER[match.collection],
                            match.entity_id,
                        ),
                    )[:3]
                    for match in ranked_matches:
                        identity = (match.collection, match.entity_id)
                        if identity in translated_identities:
                            continue
                        translated_identities.add(identity)
                        candidates.append(
                            ResolvedCandidate(
                                segment_id=segment.segment_id,
                                route="umls",
                                review_status="needs_review",
                                dictionary_match=match,
                                evidence=evidence,
                                umls_provenance=umls_provenance,
                            )
                        )

                # A typed UMLS miss has already exhausted vector search in its
                # compatible collection. Retrying the same span against every
                # vector collection is both expensive and semantically unsafe;
                # unrestricted fallback is exact-only so a linker type mistake
                # can still recover a verified local dictionary term.
                planned_regions = [
                    (start, end, True) for start, end in fallback_regions
                ]
                planned_regions.extend(
                    (start, end, True)
                    for start, end in _complement_regions(
                        len(translation), worker_intervals
                    )
                )
                seen_ngram_spans: set[tuple[int, int, str]] = set()
                for region_start, region_end, exact_only in planned_regions:
                    region_matched = False
                    planned_any = False
                    region_plans: list[_NgramQuery] = []
                    for plan in _ngram_queries_for_region(
                        translation,
                        region_start,
                        region_end,
                    ):
                        span_key = (
                            plan.evidence_span.start_char,
                            plan.evidence_span.end_char,
                            plan.search_text.casefold(),
                        )
                        if span_key in seen_ngram_spans:
                            continue
                        seen_ngram_spans.add(span_key)
                        if segment_budget <= 0 or document_budget <= 0:
                            break
                        planned_any = True
                        segment_budget -= 1
                        document_budget -= 1
                        ngram_query_count += 1
                        region_plans.append(plan)
                    region_matches = safe_search_many(
                        tuple(
                            (plan.search_text, segment.collection_hints)
                            for plan in region_plans
                        ),
                        exact_only=exact_only,
                    )
                    for plan, matches in zip(region_plans, region_matches):
                        if matches:
                            region_matched = True
                        evidence = CandidateEvidence(
                            scope="whole_raw_segment",
                            translated_query_span=plan.evidence_span,
                        )
                        for match in matches:
                            identity = (match.collection, match.entity_id)
                            if identity in translated_identities:
                                continue
                            translated_identities.add(identity)
                            candidates.append(
                                ResolvedCandidate(
                                    segment_id=segment.segment_id,
                                    route="ngram_fallback",
                                    review_status="needs_review",
                                    dictionary_match=match,
                                    evidence=evidence,
                                )
                            )
                    if planned_any and not region_matched:
                        unresolved_count += 1

        # Official RAW matches are a post-UMLS safety net. They recover exact
        # Korean canonical terms that translation/linking missed without
        # allowing the RAW lane to suppress translated candidates.
        for segment in document.segments:
            raw_started = time.perf_counter()
            try:
                raw_hits = self._dictionary.raw_matches(
                    raw_text=segment.raw_text,
                    context=context,
                )
            except Exception:
                add_issue(
                    "DICTIONARY_UNAVAILABLE",
                    "dictionary_search",
                    "baseline",
                    segment.segment_id,
                )
                raw_hits = ()
            finally:
                dictionary_ms += (time.perf_counter() - raw_started) * 1000
            translated_identities = frozenset(
                resolved_identities_by_segment[segment.segment_id]
            )
            for hit in raw_hits:
                identity = (hit.match.collection, hit.match.entity_id)
                if identity in translated_identities:
                    continue
                candidates.append(
                    ResolvedCandidate(
                        segment_id=segment.segment_id,
                        route=hit.route,
                        review_status=hit.review_status,
                        dictionary_match=hit.match,
                        evidence=CandidateEvidence(
                            scope="exact_raw_span",
                            raw_span=hit.span,
                        ),
                    )
                )

        segment_order = {
            segment.segment_id: index
            for index, segment in enumerate(document.segments)
        }
        candidates.sort(
            key=lambda candidate: self._candidate_sort_key(
                candidate,
                segment_order,
            )
        )
        return QueryResolution(
            mode="umls_primary",
            status="partial" if issues else "complete",
            policy_version=self._policy_version,
            umls_query_count=umls_query_count,
            ngram_query_count=ngram_query_count,
            unresolved_count=unresolved_count,
            candidates=tuple(candidates),
            issues=tuple(issues),
            telemetry=QueryResolutionTelemetry(
                umls_ms=round(umls_ms, 3),
                dictionary_ms=round(dictionary_ms, 3),
                vector_ms=round(vector_ms, 3),
                exact_statement_count=exact_statement_count,
                vector_statement_count=vector_statement_count,
                search_cache_hit_count=search_cache_hit_count,
                routed_query_count=routed_query_count,
                routing_conflict_count=routing_conflict_count,
                exact_search_batch_count=exact_search_batch_count,
                exact_search_query_count=exact_search_query_count,
                exact_search_hit_count=exact_search_hit_count,
                vector_fallback_batch_count=vector_fallback_batch_count,
                vector_fallback_query_count=vector_fallback_query_count,
                vector_fallback_hit_count=vector_fallback_hit_count,
                vector_fallback_empty_count=vector_fallback_empty_count,
                umls_surface_query_count=umls_surface_query_count,
                umls_canonical_query_count=umls_canonical_query_count,
                semantic_fallback_query_count=semantic_fallback_query_count,
                ngram_fallback_query_count=ngram_query_count,
                vector_collection_ms=tuple(
                    (collection, round(vector_collection_ms[collection], 3))
                    for collection in _VECTOR_COLLECTIONS
                ),
                vector_collection_statement_counts=tuple(
                    (
                        collection,
                        vector_collection_statement_counts[collection],
                    )
                    for collection in _VECTOR_COLLECTIONS
                ),
                vector_collection_batch_counts=tuple(
                    (collection, vector_collection_batch_counts[collection])
                    for collection in _VECTOR_COLLECTIONS
                ),
                vector_collection_query_counts=tuple(
                    (collection, vector_collection_query_counts[collection])
                    for collection in _VECTOR_COLLECTIONS
                ),
                vector_collection_candidate_counts=tuple(
                    (
                        collection,
                        vector_collection_candidate_counts[collection],
                    )
                    for collection in _VECTOR_COLLECTIONS
                ),
                vector_collection_empty_query_counts=tuple(
                    (
                        collection,
                        vector_collection_empty_query_counts[collection],
                    )
                    for collection in _VECTOR_COLLECTIONS
                ),
                vector_partition_ms=tuple(
                    (collection, partition, round(elapsed_ms, 3))
                    for (collection, partition), elapsed_ms
                    in vector_partition_ms.items()
                ),
                vector_partition_result_counts=tuple(
                    (collection, partition, count)
                    for (collection, partition), count
                    in vector_partition_result_counts.items()
                ),
            ),
        )

