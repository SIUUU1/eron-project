from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Literal, Sequence

from .medical_query_resolver import (
    CandidateEvidence,
    LocalDictionaryMatch,
    MedicalQueryDocument,
    MedicalQueryResolver,
    QueryResolution,
    QueryResolutionIssue,
    QueryTextSpan,
    ResolvedCandidate,
    UmlsCandidateProvenance,
)
from .retrieval import DictionaryPaths
from .vector_store import (
    MedicalHashEmbedder,
    VECTOR_DIMENSIONS,
    VECTOR_INDEX_SCHEMA_VERSION,
    dictionary_source_hashes,
)


UMLS_LINK_THRESHOLD = 0.8
MAX_QUERIES_PER_SEGMENT = 8
MAX_QUERIES_PER_DOCUMENT = 128
MAX_CANDIDATES_PER_QUERY = 5
MAX_WORKER_SEGMENTS = 50
MAX_WORKER_TRANSLATED_CHARS = 20_000
UMLS_PRIMARY_POLICY_VERSION = "umls-primary-policy-v1"

_COLLECTION_ORDER = {
    "drug_terms": 0,
    "procedure_terms": 1,
    "anatomy_terms": 2,
    "emergency_terms": 3,
    "kcd9_terms": 4,
}
_ROUTE_ORDER = {
    "raw_exact": 0,
    "approved_alias": 1,
    "raw_similarity": 2,
    "umls": 3,
    "ngram_fallback": 4,
}
_OFFICIAL_STATUSES = {"approved", "official", "verified"}
_ENGLISH_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’/-]*")
_ASCII_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
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
class _DictionaryEntity:
    collection: str
    entity_id: str
    canonical_ko: str
    canonical_en: str | None
    review_status: str


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


def _read_rows(
    path: Path,
    query: str,
    parameters: Sequence[Any],
) -> list[sqlite3.Row]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return list(connection.execute(query, tuple(parameters)))
    finally:
        connection.close()


def _dictionary_bundle_version(
    paths: DictionaryPaths,
    source_hashes: dict[str, str],
) -> str:
    named_paths = tuple(
        sorted(
            (
                ("anatomy_terms", paths.anatomy),
                ("drug_terms", paths.drug),
                ("emergency_terms", paths.emergency),
                ("kcd9_terms", paths.kcd9),
                ("procedure_terms", paths.procedure),
            )
        )
    )
    bundle = hashlib.sha256()
    for collection, path in named_paths:
        bundle.update(collection.encode("utf-8"))
        bundle.update(b"\0")
        bundle.update(path.name.encode("utf-8"))
        bundle.update(b"\0")
        bundle.update(source_hashes[collection].encode("ascii"))
        bundle.update(b"\0")
    return "sha256:" + bundle.hexdigest()


class VerifiedLocalDictionary:
    """Search local assets and re-read every selected entity from source SQLite."""

    def __init__(
        self,
        db_root: Path,
        *,
        raw_retriever: Any,
        vector_index: Path | None = None,
        minimum_vector_similarity: float = 0.38,
    ) -> None:
        if not 0.0 <= minimum_vector_similarity <= 1.0:
            raise ValueError("minimum_vector_similarity must be between zero and one")
        self.paths = DictionaryPaths.discover(Path(db_root))
        self._source_hashes = dictionary_source_hashes(self.paths)
        self.version = _dictionary_bundle_version(
            self.paths,
            self._source_hashes,
        )
        self._raw_retriever = getattr(
            raw_retriever,
            "base_retriever",
            raw_retriever,
        )
        self._alias_store = getattr(raw_retriever, "alias_store", None)
        self._vector_index = Path(vector_index) if vector_index is not None else None
        if self._vector_index is not None and not self._vector_index.is_file():
            raise ValueError(f"vector index not found: {self._vector_index}")
        self._source_stats = self._current_source_stats()
        self._vector_index_stat = (
            self._file_stat(self._vector_index)
            if self._vector_index is not None
            else None
        )
        self._verified_vector_collections = self._read_verified_vector_collections()
        self._minimum_vector_similarity = float(minimum_vector_similarity)
        self._embedder = (
            MedicalHashEmbedder()
            if self._vector_index and self._verified_vector_collections
            else None
        )

    @staticmethod
    def _file_stat(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_size, stat.st_mtime_ns

    def _source_paths(self) -> dict[str, Path]:
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

    def _read_verified_vector_collections(self) -> frozenset[str]:
        if self._vector_index is None:
            return frozenset()
        try:
            rows = _read_rows(
                self._vector_index,
                """
                SELECT collection, source_sha256, schema_version, dimensions
                  FROM vector_index_metadata
                """,
                (),
            )
        except (OSError, sqlite3.Error):
            return frozenset()
        return frozenset(
            str(row["collection"])
            for row in rows
            if str(row["collection"]) in self._source_hashes
            and str(row["source_sha256"]) == self._source_hashes[str(row["collection"])]
            and str(row["schema_version"]) == VECTOR_INDEX_SCHEMA_VERSION
            and row["dimensions"] == VECTOR_DIMENSIONS
        )

    def _current_vector_collections(self) -> frozenset[str]:
        if (
            self._vector_index is None
            or self._file_stat(self._vector_index) != self._vector_index_stat
            or not self._assets_are_current()
        ):
            return frozenset()
        return self._verified_vector_collections

    def _lookup_uncached(
        self,
        collection: str,
        entity_id: str,
    ) -> _DictionaryEntity | None:
        try:
            if collection == "drug_terms":
                prefix, kind, local_id = entity_id.split(":", 2)
                if prefix != "drug" or kind not in {"ingredient", "product"}:
                    return None
                if kind == "ingredient":
                    rows = _read_rows(
                        self.paths.drug,
                        """
                        SELECT canonical_ko, canonical_en, 'official' review_status
                          FROM ingredients WHERE CAST(ingredient_id AS TEXT)=?
                          LIMIT 1
                        """,
                        (local_id,),
                    )
                else:
                    rows = _read_rows(
                        self.paths.drug,
                        """
                        SELECT product_name_ko canonical_ko,
                               product_name_en canonical_en,
                               'official' review_status
                          FROM products WHERE CAST(item_id AS TEXT)=?
                          LIMIT 1
                        """,
                        (local_id,),
                    )
            elif collection == "procedure_terms":
                prefix, local_id = entity_id.split(":", 1)
                if prefix != "procedure":
                    return None
                rows = _read_rows(
                    self.paths.procedure,
                    """
                    SELECT canonical_name_ko canonical_ko,
                           canonical_name_en canonical_en,
                           review_status
                      FROM clinical_terms WHERE CAST(term_id AS TEXT)=?
                      LIMIT 1
                    """,
                    (local_id,),
                )
            elif collection == "anatomy_terms":
                prefix, local_id = entity_id.split(":", 1)
                if prefix != "anatomy":
                    return None
                rows = _read_rows(
                    self.paths.anatomy,
                    """
                    SELECT korean_name canonical_ko, english_name canonical_en,
                           verification_status review_status
                      FROM anatomical_terms WHERE CAST(term_id AS TEXT)=?
                      LIMIT 1
                    """,
                    (local_id,),
                )
            elif collection == "emergency_terms":
                prefix, local_id = entity_id.split(":", 1)
                if prefix != "emergency":
                    return None
                rows = _read_rows(
                    self.paths.emergency,
                    """
                    SELECT standard_ko canonical_ko, standard_en canonical_en,
                           review_status
                      FROM terms WHERE CAST(term_id AS TEXT)=?
                      LIMIT 1
                    """,
                    (local_id,),
                )
            elif collection == "kcd9_terms":
                prefix, local_id = entity_id.split(":", 1)
                if prefix != "kcd":
                    return None
                rows = _read_rows(
                    self.paths.kcd9,
                    """
                    SELECT canonical_ko_name canonical_ko,
                           canonical_en_name canonical_en,
                           'official' review_status
                      FROM kcd_codes
                     WHERE code=? AND is_complete=1 LIMIT 1
                    """,
                    (local_id,),
                )
            else:
                return None
        except (OSError, sqlite3.Error, ValueError):
            return None
        if not rows:
            return None
        row = rows[0]
        canonical_ko = str(row["canonical_ko"] or "").strip()
        canonical_en = str(row["canonical_en"] or "").strip() or None
        if not canonical_ko:
            return None
        return _DictionaryEntity(
            collection=collection,
            entity_id=entity_id,
            canonical_ko=canonical_ko,
            canonical_en=canonical_en,
            review_status=str(row["review_status"] or "").casefold(),
        )

    def lookup(
        self,
        collection: str,
        entity_id: str,
    ) -> _DictionaryEntity | None:
        self._ensure_assets_are_current()
        return self._lookup_uncached(collection, entity_id)

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
        self._ensure_assets_are_current()
        raw_candidates = self._raw_retriever.retrieve(
            raw_text=raw_text,
            context=context,
        )
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
                            if entity.review_status in _OFFICIAL_STATUSES
                            and str(
                                candidate.get("review_status") or ""
                            ).casefold() in _OFFICIAL_STATUSES
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
            except (OSError, sqlite3.Error):
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
                rows = _read_rows(path, sql, parameters)
            except (OSError, sqlite3.Error):
                continue
            identities.extend(
                (collection, str(row["entity_id"])) for row in rows
            )
        return identities

    def search(
        self,
        query_text: str,
        *,
        limit: int = MAX_CANDIDATES_PER_QUERY,
        collections: Iterable[str] | None = None,
    ) -> tuple[LocalDictionaryMatch, ...]:
        self._ensure_assets_are_current()
        if not isinstance(query_text, str) or not query_text.strip():
            return ()
        bounded_limit = min(MAX_CANDIDATES_PER_QUERY, max(0, int(limit)))
        selected_collections = frozenset(
            collections
            if collections is not None
            else (
                "drug_terms",
                "procedure_terms",
                "anatomy_terms",
                "emergency_terms",
            )
        ) & frozenset(
            {
                "drug_terms",
                "procedure_terms",
                "anatomy_terms",
                "emergency_terms",
            }
        )
        if not selected_collections:
            return ()
        matches: dict[tuple[str, str], tuple[LocalDictionaryMatch, bool]] = {}
        for collection, entity_id in self._exact_query_identities(query_text):
            if collection not in selected_collections:
                continue
            match = self._match(collection, entity_id, 1.0)
            if match is not None:
                matches.setdefault((collection, entity_id), (match, True))
        for collection, entity_id, score in self._vector_query_identities(
            query_text,
            limit=max(MAX_CANDIDATES_PER_QUERY * 4, bounded_limit * 4),
            collections=selected_collections,
        ):
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
        return tuple(
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

    def search_exact(
        self,
        query_text: str,
        *,
        limit: int = MAX_CANDIDATES_PER_QUERY,
        collections: Iterable[str] | None = None,
    ) -> tuple[LocalDictionaryMatch, ...]:
        self._ensure_assets_are_current()
        if not isinstance(query_text, str) or not query_text.strip():
            return ()
        bounded_limit = min(MAX_CANDIDATES_PER_QUERY, max(0, int(limit)))
        selected_collections = frozenset(
            collections
            if collections is not None
            else (
                "drug_terms",
                "procedure_terms",
                "anatomy_terms",
                "emergency_terms",
            )
        ) & frozenset(
            {
                "drug_terms",
                "procedure_terms",
                "anatomy_terms",
                "emergency_terms",
            }
        )
        if not selected_collections:
            return ()
        matches: dict[tuple[str, str], LocalDictionaryMatch] = {}
        for collection, entity_id in self._exact_query_identities(query_text):
            if collection not in selected_collections:
                continue
            match = self._match(collection, entity_id, 1.0)
            if match is not None:
                matches.setdefault((collection, entity_id), match)
        return tuple(
            sorted(
                matches.values(),
                key=lambda match: (
                    _COLLECTION_ORDER[match.collection],
                    match.entity_id,
                ),
            )[:bounded_limit]
        )

    def _vector_query_identities(
        self,
        query_text: str,
        *,
        limit: int,
        collections: frozenset[str],
    ) -> list[tuple[str, str, float]]:
        active_collections = collections & self._current_vector_collections()
        if (
            self._vector_index is None
            or self._embedder is None
            or limit <= 0
            or not active_collections
        ):
            return []
        query_vector = self._embedder.embed(query_text)
        try:
            import numpy as np
            import sqlite_vec

            if not np.any(query_vector):
                return []
            connection = sqlite3.connect(
                f"file:{self._vector_index}?mode=ro",
                uri=True,
            )
            connection.row_factory = sqlite3.Row
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            connection.enable_load_extension(False)
        except (ImportError, OSError, sqlite3.Error):
            return []
        identities: list[tuple[str, str, float]] = []
        query_tokens = {
            token.casefold()
            for token in _ASCII_QUERY_TOKEN_RE.findall(query_text)
            if len(token) >= 3
        }
        try:
            for collection in sorted(
                active_collections,
                key=_COLLECTION_ORDER.__getitem__,
            ):
                entity_types: tuple[str | None, ...] = (
                    ("ingredient", "product")
                    if collection == "drug_terms"
                    else (None,)
                )
                for entity_type in entity_types:
                    partition = (
                        " AND entity_type=?" if entity_type is not None else ""
                    )
                    parameters: tuple[Any, ...] = (
                        (query_vector, limit, entity_type)
                        if entity_type is not None
                        else (query_vector, limit)
                    )
                    try:
                        rows = connection.execute(
                            f'''SELECT entity_id, source_text, canonical_en, distance
                                  FROM "{collection}"
                                 WHERE embedding MATCH ? AND k = ?{partition}''',
                            parameters,
                        ).fetchall()
                    except sqlite3.Error:
                        continue
                    for row in rows:
                        row_tokens = {
                            token.casefold()
                            for token in _ASCII_QUERY_TOKEN_RE.findall(
                                " ".join(
                                    (
                                        str(row["source_text"] or ""),
                                        str(row["canonical_en"] or ""),
                                    )
                                )
                            )
                            if len(token) >= 3
                        }
                        if query_tokens and not query_tokens & row_tokens:
                            continue
                        distance = row["distance"]
                        if (
                            isinstance(distance, bool)
                            or not isinstance(distance, (int, float))
                            or not math.isfinite(distance)
                        ):
                            continue
                        similarity = 1.0 - float(distance)
                        if similarity < self._minimum_vector_similarity:
                            continue
                        identities.append(
                            (
                                collection,
                                str(row["entity_id"]),
                                min(1.0, max(0.0, similarity)),
                            )
                        )
        finally:
            connection.close()
        identities.sort(
            key=lambda item: (
                -item[2],
                _COLLECTION_ORDER[item[0]],
                item[1],
            )
        )
        return identities


class UmlsPrimaryMedicalQueryResolver(MedicalQueryResolver):
    def __init__(
        self,
        *,
        dictionary: VerifiedLocalDictionary,
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
        candidates: list[ResolvedCandidate] = []
        issues: list[QueryResolutionIssue] = []
        issue_keys: set[tuple[str, str, str, str | None]] = set()

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

        def safe_search(
            query_text: str,
            allowed_collections: frozenset[str] | None = None,
        ) -> tuple[LocalDictionaryMatch, ...]:
            try:
                values = self._dictionary.search(
                    query_text,
                    limit=MAX_CANDIDATES_PER_QUERY,
                    collections=allowed_collections,
                )
            except Exception:
                add_issue(
                    "DICTIONARY_UNAVAILABLE",
                    "dictionary_search",
                    "baseline",
                )
                return ()
            return _bounded_dictionary_matches(
                value
                for value in values
                if allowed_collections is None
                or (
                    isinstance(value, LocalDictionaryMatch)
                    and value.collection in allowed_collections
                )
            )

        def safe_exact_search(
            query_text: str,
            allowed_collections: frozenset[str] | None = None,
        ) -> tuple[LocalDictionaryMatch, ...]:
            try:
                search_exact = getattr(self._dictionary, "search_exact", None)
                if callable(search_exact):
                    values = search_exact(
                        query_text,
                        limit=MAX_CANDIDATES_PER_QUERY,
                        collections=allowed_collections,
                    )
                else:
                    values = tuple(
                        match
                        for match in self._dictionary.search(
                            query_text,
                            limit=MAX_CANDIDATES_PER_QUERY,
                            collections=allowed_collections,
                        )
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
            return _bounded_dictionary_matches(
                value
                for value in values
                if allowed_collections is None
                or (
                    isinstance(value, LocalDictionaryMatch)
                    and value.collection in allowed_collections
                )
            )

        context = [
            {"id": segment.segment_id, "text": segment.raw_text}
            for segment in document.segments
        ]
        raw_identities_by_segment: dict[str, set[tuple[str, str]]] = {
            segment.segment_id: set() for segment in document.segments
        }
        for segment in document.segments:
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
            for hit in raw_hits:
                raw_identities_by_segment[segment.segment_id].add(
                    (hit.match.collection, hit.match.entity_id)
                )
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
                try:
                    outcome = self._span_linker.link(
                        list(batch),
                        lane="clinical",
                    )
                except Exception:
                    outcome = None
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
                translated_identities = set(
                    raw_identities_by_segment[segment.segment_id]
                )
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
                    allowed_collections = _collections_for_semantic_types(
                        top_concept.get("semantic_types")
                    )
                    query_variants: list[str] = []
                    surface_query = str(
                        source_span.get("surface_query") or ""
                    ).strip()
                    for value in (
                        surface_query,
                        str(top_concept["canonical_name"]).strip(),
                    ):
                        if not value:
                            continue
                        if value.casefold() not in {
                            query.casefold() for query in query_variants
                        }:
                            query_variants.append(value)

                    verified_matches: dict[tuple[str, str], LocalDictionaryMatch] = {}
                    for query in query_variants:
                        if segment_budget <= 0 or document_budget <= 0:
                            break
                        segment_budget -= 1
                        document_budget -= 1
                        umls_query_count += 1
                        for match in safe_search(query, allowed_collections):
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
                    for match in verified_matches.values():
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

                planned_regions = [
                    (start, end, False) for start, end in fallback_regions
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
                        matches = (
                            safe_exact_search(plan.search_text)
                            if exact_only
                            else safe_search(plan.search_text)
                        )
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
        )

