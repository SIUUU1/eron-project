"""Medical vector search seam with SQLite, PostgreSQL, and shadow adapters."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import logging
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Protocol, Sequence

from .medical_vector_contract import (
    MEDICAL_VECTOR_COLLECTIONS,
    MEDICAL_VECTOR_MODEL_VERSION,
    VECTOR_DIMENSIONS,
    VECTOR_INDEX_SCHEMA_VERSION,
)
from .terminology_repository import _normalize_database_url
from .vector_store import MedicalHashEmbedder


LOGGER = logging.getLogger(__name__)
_COLLECTION_ORDER = {
    collection: index
    for index, collection in enumerate(MEDICAL_VECTOR_COLLECTIONS)
}
_ASCII_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True, slots=True)
class VectorIdentity:
    collection: str
    entity_id: str
    similarity: float


@dataclass(frozen=True, slots=True)
class VectorIdentityBatch:
    identities: tuple[tuple[VectorIdentity, ...], ...]
    elapsed_ms: float
    statement_count: int


class MedicalVectorRepository(Protocol):
    version: str

    def request_session(self): ...

    def search_many(
        self,
        requests: Sequence[tuple[str, frozenset[str]]],
        *,
        limit: int,
        skip_collections_by_index: dict[int, frozenset[str]] | None = None,
    ) -> VectorIdentityBatch: ...


def _empty_batch(size: int, *, elapsed_ms: float = 0.0) -> VectorIdentityBatch:
    return VectorIdentityBatch(
        identities=tuple(() for _ in range(size)),
        elapsed_ms=round(elapsed_ms, 3),
        statement_count=0,
    )


def _tokens(value: object) -> set[str]:
    return {
        token.casefold()
        for token in _ASCII_QUERY_TOKEN_RE.findall(str(value or ""))
        if len(token) >= 3
    }


def _normalize_requests(
    requests: Sequence[tuple[str, frozenset[str]]],
) -> tuple[tuple[str, frozenset[str]], ...]:
    if len(requests) > 64:
        raise ValueError("medical vector batch may contain at most 64 queries")
    supported = frozenset(MEDICAL_VECTOR_COLLECTIONS)
    normalized: list[tuple[str, frozenset[str]]] = []
    for query_text, collections in requests:
        if not isinstance(query_text, str):
            raise TypeError("medical vector query text must be a string")
        normalized.append((query_text.strip(), frozenset(collections) & supported))
    return tuple(normalized)


def _sorted_identities(
    values: list[list[VectorIdentity]],
) -> tuple[tuple[VectorIdentity, ...], ...]:
    return tuple(
        tuple(sorted(
            identities,
            key=lambda item: (
                -item.similarity,
                _COLLECTION_ORDER[item.collection],
                item.entity_id,
            ),
        ))
        for identities in values
    )


@dataclass(slots=True)
class _SqliteSession:
    connection: sqlite3.Connection
    depth: int = 1


class SqliteMedicalVectorRepository:
    """Read a verified immutable sqlite-vec medical index."""

    def __init__(
        self,
        index_path: Path,
        *,
        source_hashes: dict[str, str],
        minimum_similarity: float = 0.38,
    ) -> None:
        self._index_path = Path(index_path)
        if not self._index_path.is_file():
            raise ValueError(f"vector index not found: {self._index_path}")
        self._source_hashes = dict(source_hashes)
        self._minimum_similarity = float(minimum_similarity)
        self._local = threading.local()
        self._embedder = MedicalHashEmbedder()
        stat = self._index_path.stat()
        self._index_stat = (stat.st_size, stat.st_mtime_ns)
        with self.request_session():
            rows = self._execute(
                """
                SELECT collection, source_sha256, schema_version, dimensions
                  FROM vector_index_metadata
                """,
                (),
            )
        self._active_collections = frozenset(
            str(row["collection"])
            for row in rows
            if str(row["collection"]) in MEDICAL_VECTOR_COLLECTIONS
            and self._source_hashes.get(str(row["collection"]))
                == str(row["source_sha256"])
            and str(row["schema_version"]) == VECTOR_INDEX_SCHEMA_VERSION
            and int(row["dimensions"]) == VECTOR_DIMENSIONS
        )
        digest = hashlib.sha256()
        for collection in sorted(self._active_collections):
            digest.update(collection.encode("utf-8"))
            digest.update(b"\0")
            digest.update(self._source_hashes[collection].encode("ascii"))
            digest.update(b"\0")
        self.version = "sqlite-vector:sha256:" + digest.hexdigest()

    @staticmethod
    def _connect(index_path: Path) -> sqlite3.Connection:
        import sqlite_vec

        connection = sqlite3.connect(
            f"file:{index_path.resolve()}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
        return connection

    @contextmanager
    def request_session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = _SqliteSession(self._connect(self._index_path))
            self._local.session = session
        else:
            session.depth += 1
        try:
            yield self
        finally:
            session.depth -= 1
            if session.depth == 0:
                session.connection.close()
                del self._local.session

    def _execute(
        self,
        sql: str,
        parameters: Sequence[Any],
    ) -> list[sqlite3.Row]:
        session = getattr(self._local, "session", None)
        if session is None:
            raise RuntimeError("medical vector access requires a request session")
        return list(session.connection.execute(sql, tuple(parameters)))

    def _is_current(self) -> bool:
        try:
            stat = self._index_path.stat()
        except OSError:
            return False
        return (stat.st_size, stat.st_mtime_ns) == self._index_stat

    def search_many(
        self,
        requests: Sequence[tuple[str, frozenset[str]]],
        *,
        limit: int,
        skip_collections_by_index: dict[int, frozenset[str]] | None = None,
    ) -> VectorIdentityBatch:
        normalized = _normalize_requests(requests)
        if limit <= 0 or not normalized or not self._active_collections:
            return _empty_batch(len(normalized))
        started = time.perf_counter()
        if not self._is_current():
            return _empty_batch(
                len(normalized),
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        skipped = skip_collections_by_index or {}
        output: list[list[VectorIdentity]] = [[] for _ in normalized]
        query_tokens = [_tokens(query_text) for query_text, _ in normalized]
        query_vectors = [
            self._embedder.embed(query_text) if query_text else None
            for query_text, _ in normalized
        ]
        statement_count = 0
        with self.request_session():
            for collection in MEDICAL_VECTOR_COLLECTIONS:
                if collection not in self._active_collections:
                    continue
                for index, (_, selected) in enumerate(normalized):
                    vector = query_vectors[index]
                    if (
                        collection not in selected
                        or collection in skipped.get(index, frozenset())
                        or vector is None
                        or not vector.any()
                    ):
                        continue
                    entity_types = (
                        ("ingredient", "product")
                        if collection == "drug_terms"
                        else (None,)
                    )
                    for entity_type in entity_types:
                        partition = (
                            " AND entity_type=?" if entity_type is not None else ""
                        )
                        parameters: tuple[Any, ...] = (
                            (vector, limit, entity_type)
                            if entity_type is not None
                            else (vector, limit)
                        )
                        try:
                            rows = self._execute(
                                f'''SELECT entity_id, source_text, canonical_en,
                                           distance
                                      FROM "{collection}"
                                     WHERE embedding MATCH ? AND k = ?{partition}''',
                                parameters,
                            )
                        except sqlite3.Error:
                            continue
                        statement_count += 1
                        self._append_rows(
                            output[index],
                            collection,
                            rows,
                            query_tokens[index],
                        )
        return VectorIdentityBatch(
            identities=_sorted_identities(output),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            statement_count=statement_count,
        )

    def _append_rows(
        self,
        output: list[VectorIdentity],
        collection: str,
        rows: Sequence[Any],
        query_tokens: set[str],
    ) -> None:
        for row in rows:
            row_tokens = _tokens(
                f"{row['source_text'] or ''} {row['canonical_en'] or ''}"
            )
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
            if similarity < self._minimum_similarity:
                continue
            output.append(VectorIdentity(
                collection,
                str(row["entity_id"]),
                min(1.0, max(0.0, similarity)),
            ))


@dataclass(slots=True)
class _PostgresSession:
    connection: Any
    depth: int = 1


class PostgresMedicalVectorRepository:
    """Batch KNN search over active collection-scoped pgvector releases."""

    def __init__(
        self,
        database_url: str,
        *,
        minimum_similarity: float = 0.38,
    ) -> None:
        cleaned = _normalize_database_url(database_url)
        if not cleaned:
            raise ValueError("PostgreSQL medical vector repository requires a URL")
        self._database_url = cleaned
        self._minimum_similarity = float(minimum_similarity)
        self._local = threading.local()
        self._embedder = MedicalHashEmbedder()
        with self.request_session():
            rows = self._execute(
                """
                SELECT metadata->>'collection', source_id, version, content_hash,
                       metadata->>'model_version', metadata->>'dimensions'
                  FROM clinicalnlp.source_releases vr
                 WHERE vr.source_kind='VECTOR' AND vr.is_active
                   AND vr.source_id LIKE 'medical_vector:%%'
                   AND EXISTS (
                       SELECT 1
                         FROM clinicalnlp.medical_vectors v
                         JOIN clinicalnlp.medical_concepts c
                           ON c.concept_pk=v.concept_pk
                         JOIN clinicalnlp.source_releases dr
                           ON dr.release_id=c.source_release_id
                        WHERE v.vector_release_id=vr.release_id
                          AND dr.is_active
                          AND dr.source_kind='MEDICAL_DICTIONARY'
                          AND dr.content_hash=
                              vr.metadata->>'dictionary_source_sha256'
                          AND c.collection_name=
                              vr.metadata->>'collection'
                   )
                 ORDER BY source_id
                """,
                (),
            )
        active = {
            str(row[0]): row
            for row in rows
            if str(row[0]) in MEDICAL_VECTOR_COLLECTIONS
            and str(row[4]) == MEDICAL_VECTOR_MODEL_VERSION
            and str(row[5]) == str(VECTOR_DIMENSIONS)
        }
        missing = set(MEDICAL_VECTOR_COLLECTIONS) - set(active)
        if missing:
            raise RuntimeError(
                f"PostgreSQL medical vector releases are not ready: {sorted(missing)}"
            )
        self._active_collections = frozenset(active)
        digest = hashlib.sha256()
        for collection in MEDICAL_VECTOR_COLLECTIONS:
            digest.update("\0".join(str(value) for value in active[collection][1:4]).encode("utf-8"))
            digest.update(b"\0")
        self.version = "postgres-vector:sha256:" + digest.hexdigest()

    @staticmethod
    def _connect(database_url: str):
        import psycopg

        return psycopg.connect(database_url, autocommit=True)

    @contextmanager
    def request_session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = _PostgresSession(self._connect(self._database_url))
            self._local.session = session
        else:
            session.depth += 1
        try:
            yield self
        finally:
            session.depth -= 1
            if session.depth == 0:
                session.connection.close()
                del self._local.session

    def _execute(self, sql: str, parameters: Sequence[Any]) -> list[tuple[Any, ...]]:
        session = getattr(self._local, "session", None)
        if session is None:
            raise RuntimeError("medical vector access requires a request session")
        with session.connection.cursor() as cursor:
            cursor.execute(sql, tuple(parameters))
            return list(cursor.fetchall())

    def search_many(
        self,
        requests: Sequence[tuple[str, frozenset[str]]],
        *,
        limit: int,
        skip_collections_by_index: dict[int, frozenset[str]] | None = None,
    ) -> VectorIdentityBatch:
        normalized = _normalize_requests(requests)
        if limit <= 0 or not normalized:
            return _empty_batch(len(normalized))
        started = time.perf_counter()
        skipped = skip_collections_by_index or {}
        query_tokens = [_tokens(query_text) for query_text, _ in normalized]
        query_vectors = [
            self._embedder.embed(query_text) if query_text else None
            for query_text, _ in normalized
        ]
        output: list[list[VectorIdentity]] = [[] for _ in normalized]
        statement_count = 0
        with self.request_session():
            for collection in MEDICAL_VECTOR_COLLECTIONS:
                partitions = (
                    ("ingredient", "product")
                    if collection == "drug_terms"
                    else (None,)
                )
                for entity_type in partitions:
                    indexes = [
                        index
                        for index, (query_text, selected) in enumerate(normalized)
                        if query_text
                        and collection in selected
                        and collection not in skipped.get(index, frozenset())
                        and query_vectors[index] is not None
                        and query_vectors[index].any()
                    ]
                    if not indexes:
                        continue
                    vector_literals = [
                        "[" + ",".join(
                            format(float(value), ".9g")
                            for value in query_vectors[index]
                        ) + "]"
                        for index in indexes
                    ]
                    rows = self._execute(
                        """
                        WITH queries AS (
                            SELECT *
                              FROM unnest(%s::integer[], %s::text[])
                                   AS q(query_index, embedding_text)
                        )
                        SELECT q.query_index, hit.entity_id, hit.source_text,
                               hit.canonical_en, hit.similarity
                          FROM queries q
                          CROSS JOIN LATERAL (
                              SELECT c.entity_id, v.source_text, c.canonical_en,
                                     1 - (v.embedding <=>
                                          q.embedding_text::vector(256)) similarity
                                FROM clinicalnlp.medical_vectors v
                                JOIN clinicalnlp.source_releases vr
                                  ON vr.release_id=v.vector_release_id
                                JOIN clinicalnlp.medical_concepts c
                                  ON c.concept_pk=v.concept_pk
                                JOIN clinicalnlp.source_releases dr
                                  ON dr.release_id=c.source_release_id
                               WHERE vr.is_active AND vr.source_kind='VECTOR'
                                 AND vr.source_id=%s
                                 AND dr.is_active
                                 AND dr.source_kind='MEDICAL_DICTIONARY'
                                 AND c.collection_name=%s
                                 AND (%s::text IS NULL OR c.entity_type=%s)
                               ORDER BY v.embedding <=>
                                        q.embedding_text::vector(256)
                               LIMIT %s
                          ) hit
                        """,
                        (
                            indexes,
                            vector_literals,
                            f"medical_vector:{collection}",
                            collection,
                            entity_type,
                            entity_type,
                            int(limit),
                        ),
                    )
                    statement_count += 1
                    for index, entity_id, source_text, canonical_en, similarity in rows:
                        row_tokens = _tokens(f"{source_text or ''} {canonical_en or ''}")
                        if query_tokens[int(index)] and not (
                            query_tokens[int(index)] & row_tokens
                        ):
                            continue
                        if similarity is None or not math.isfinite(float(similarity)):
                            continue
                        score = float(similarity)
                        if score < self._minimum_similarity:
                            continue
                        output[int(index)].append(VectorIdentity(
                            collection,
                            str(entity_id),
                            min(1.0, max(0.0, score)),
                        ))
        return VectorIdentityBatch(
            identities=_sorted_identities(output),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            statement_count=statement_count,
        )


class ShadowMedicalVectorRepository:
    """Compare PostgreSQL vector identities while returning SQLite unchanged."""

    def __init__(
        self,
        primary: MedicalVectorRepository,
        secondary: MedicalVectorRepository,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.version = primary.version
        self._mismatch_count = 0
        self._lock = threading.Lock()

    @property
    def mismatch_count(self) -> int:
        with self._lock:
            return self._mismatch_count

    def _mismatch(self, operation: str) -> None:
        with self._lock:
            self._mismatch_count += 1
        LOGGER.warning("medical vector shadow mismatch operation=%s", operation)

    @contextmanager
    def request_session(self):
        with ExitStack() as stack:
            stack.enter_context(self.primary.request_session())
            try:
                stack.enter_context(self.secondary.request_session())
            except Exception:
                self._mismatch("session_error")
            yield self

    def search_many(
        self,
        requests: Sequence[tuple[str, frozenset[str]]],
        *,
        limit: int,
        skip_collections_by_index: dict[int, frozenset[str]] | None = None,
    ) -> VectorIdentityBatch:
        primary = self.primary.search_many(
            requests,
            limit=limit,
            skip_collections_by_index=skip_collections_by_index,
        )
        try:
            secondary = self.secondary.search_many(
                requests,
                limit=limit,
                skip_collections_by_index=skip_collections_by_index,
            )
        except Exception:
            self._mismatch("search_error")
        else:
            primary_keys = tuple(tuple(
                (item.collection, item.entity_id, round(item.similarity, 6))
                for item in values
            ) for values in primary.identities)
            secondary_keys = tuple(tuple(
                (item.collection, item.entity_id, round(item.similarity, 6))
                for item in values
            ) for values in secondary.identities)
            if primary_keys != secondary_keys:
                self._mismatch("search")
        return primary


class UnavailableMedicalVectorRepository:
    version = "medical-vector:unavailable"

    @contextmanager
    def request_session(self):
        yield self

    def search_many(
        self,
        requests: Sequence[tuple[str, frozenset[str]]],
        *,
        limit: int,
        skip_collections_by_index: dict[int, frozenset[str]] | None = None,
    ) -> VectorIdentityBatch:
        del limit, skip_collections_by_index
        return _empty_batch(len(requests))


def create_medical_vector_repository(
    *,
    mode: str,
    index_path: Path,
    source_hashes: dict[str, str],
    database_url: str,
    minimum_similarity: float = 0.38,
) -> MedicalVectorRepository:
    normalized = mode.strip().casefold()
    if normalized == "sqlite":
        return SqliteMedicalVectorRepository(
            index_path,
            source_hashes=source_hashes,
            minimum_similarity=minimum_similarity,
        )
    if normalized not in {"shadow", "postgres"}:
        raise ValueError("medical vector backend must be sqlite, shadow, or postgres")
    if normalized == "postgres":
        return PostgresMedicalVectorRepository(
            database_url,
            minimum_similarity=minimum_similarity,
        )
    primary = SqliteMedicalVectorRepository(
        index_path,
        source_hashes=source_hashes,
        minimum_similarity=minimum_similarity,
    )
    try:
        secondary: MedicalVectorRepository = PostgresMedicalVectorRepository(
            database_url,
            minimum_similarity=minimum_similarity,
        )
    except Exception:
        LOGGER.warning("medical vector shadow secondary unavailable")
        secondary = UnavailableMedicalVectorRepository()
    return ShadowMedicalVectorRepository(primary, secondary)
