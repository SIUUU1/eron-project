from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import sqlite3
import threading
import unicodedata
from typing import Any, Protocol, Sequence

from .retrieval import DictionaryPaths


LOGGER = logging.getLogger(__name__)
SUPPORTED_COLLECTIONS = frozenset(
    {
        "drug_terms",
        "procedure_terms",
        "anatomy_terms",
        "emergency_terms",
        "kcd9_terms",
    }
)
_COLLECTION_ORDER = {
    "drug_terms": 0,
    "procedure_terms": 1,
    "anatomy_terms": 2,
    "emergency_terms": 3,
    "kcd9_terms": 4,
}


def normalize_terminology_query(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(normalized.split())


@dataclass(frozen=True, slots=True)
class TerminologyIdentity:
    collection: str
    entity_id: str


@dataclass(frozen=True, slots=True)
class TerminologyEntity:
    collection: str
    entity_id: str
    canonical_ko: str
    canonical_en: str | None
    review_status: str


@dataclass(frozen=True, slots=True)
class ExactIdentityBatch:
    identities: tuple[tuple[TerminologyIdentity, ...], ...]
    statement_count: int


class TerminologyRepository(Protocol):
    version: str

    def request_session(self): ...

    def lookup(self, collection: str, entity_id: str) -> TerminologyEntity | None: ...

    def exact_identities_many(
        self,
        requests: Sequence[tuple[str, frozenset[str]]],
    ) -> ExactIdentityBatch: ...


@dataclass(slots=True)
class _SqliteSession:
    connections: dict[Path, sqlite3.Connection]
    depth: int = 1


def _local_source_paths(paths: DictionaryPaths) -> dict[str, Path]:
    return {
        "drug_terms": paths.drug,
        "procedure_terms": paths.procedure,
        "anatomy_terms": paths.anatomy,
        "emergency_terms": paths.emergency,
        "kcd9_terms": paths.kcd9,
    }


def _local_source_hashes(paths: DictionaryPaths) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for collection, path in _local_source_paths(paths).items():
        file_digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                file_digest.update(chunk)
        hashes[collection] = file_digest.hexdigest()
    return hashes


def _sqlite_bundle_version(
    paths: DictionaryPaths,
    source_hashes: dict[str, str],
) -> str:
    named_paths = tuple(sorted((
        ("drug_terms", paths.drug),
        ("procedure_terms", paths.procedure),
        ("anatomy_terms", paths.anatomy),
        ("emergency_terms", paths.emergency),
        ("kcd9_terms", paths.kcd9),
    )))
    bundle = hashlib.sha256()
    for collection, path in named_paths:
        bundle.update(collection.encode("utf-8"))
        bundle.update(b"\0")
        bundle.update(path.name.encode("utf-8"))
        bundle.update(b"\0")
        bundle.update(source_hashes[collection].encode("ascii"))
        bundle.update(b"\0")
    # Preserve the pre-repository candidate contract in default SQLite mode.
    return "sha256:" + bundle.hexdigest()


class SqliteTerminologyRepository:
    """Read verified terminology identities from the immutable SQLite sources."""

    def __init__(self, db_root: Path) -> None:
        self.paths = DictionaryPaths.discover(Path(db_root))
        self.local_source_hashes = _local_source_hashes(self.paths)
        self.version = _sqlite_bundle_version(self.paths, self.local_source_hashes)
        self._local = threading.local()
        self._entity_cache: dict[tuple[str, str], TerminologyEntity | None] = {}
        self._entity_cache_lock = threading.Lock()

    @contextmanager
    def request_session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = _SqliteSession(connections={})
            self._local.session = session
        else:
            session.depth += 1
        try:
            yield self
        finally:
            session.depth -= 1
            if session.depth == 0:
                for connection in session.connections.values():
                    connection.close()
                del self._local.session

    def _connection(self, path: Path) -> sqlite3.Connection:
        session = getattr(self._local, "session", None)
        if session is None:
            raise RuntimeError("terminology access requires a request session")
        resolved = path.resolve()
        connection = session.connections.get(resolved)
        if connection is None:
            connection = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            session.connections[resolved] = connection
        return connection

    def _rows(
        self,
        path: Path,
        query: str,
        parameters: Sequence[Any],
    ) -> list[sqlite3.Row]:
        return list(self._connection(path).execute(query, tuple(parameters)))

    def _lookup_uncached(
        self,
        collection: str,
        entity_id: str,
    ) -> TerminologyEntity | None:
        with self.request_session():
            try:
                if collection == "drug_terms":
                    prefix, kind, local_id = entity_id.split(":", 2)
                    if prefix != "drug" or kind not in {"ingredient", "product"}:
                        return None
                    if kind == "ingredient":
                        rows = self._rows(
                            self.paths.drug,
                            """
                            SELECT canonical_ko, canonical_en,
                                   'official' review_status
                              FROM ingredients
                             WHERE CAST(ingredient_id AS TEXT)=? LIMIT 1
                            """,
                            (local_id,),
                        )
                    else:
                        rows = self._rows(
                            self.paths.drug,
                            """
                            SELECT product_name_ko canonical_ko,
                                   product_name_en canonical_en,
                                   'official' review_status
                              FROM products
                             WHERE CAST(item_id AS TEXT)=? LIMIT 1
                            """,
                            (local_id,),
                        )
                elif collection == "procedure_terms":
                    prefix, local_id = entity_id.split(":", 1)
                    if prefix != "procedure":
                        return None
                    rows = self._rows(
                        self.paths.procedure,
                        """
                        SELECT canonical_name_ko canonical_ko,
                               canonical_name_en canonical_en, review_status
                          FROM clinical_terms
                         WHERE CAST(term_id AS TEXT)=? LIMIT 1
                        """,
                        (local_id,),
                    )
                elif collection == "anatomy_terms":
                    prefix, local_id = entity_id.split(":", 1)
                    if prefix != "anatomy":
                        return None
                    rows = self._rows(
                        self.paths.anatomy,
                        """
                        SELECT korean_name canonical_ko,
                               english_name canonical_en,
                               verification_status review_status
                          FROM anatomical_terms
                         WHERE CAST(term_id AS TEXT)=? LIMIT 1
                        """,
                        (local_id,),
                    )
                elif collection == "emergency_terms":
                    prefix, local_id = entity_id.split(":", 1)
                    if prefix != "emergency":
                        return None
                    rows = self._rows(
                        self.paths.emergency,
                        """
                        SELECT standard_ko canonical_ko,
                               standard_en canonical_en, review_status
                          FROM terms
                         WHERE CAST(term_id AS TEXT)=? LIMIT 1
                        """,
                        (local_id,),
                    )
                elif collection == "kcd9_terms":
                    prefix, local_id = entity_id.split(":", 1)
                    if prefix != "kcd":
                        return None
                    rows = self._rows(
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
            return _entity_from_row(collection, entity_id, rows[0] if rows else None)

    def lookup(self, collection: str, entity_id: str) -> TerminologyEntity | None:
        key = (collection, entity_id)
        with self._entity_cache_lock:
            if key in self._entity_cache:
                return self._entity_cache[key]
        entity = self._lookup_uncached(collection, entity_id)
        with self._entity_cache_lock:
            self._entity_cache.setdefault(key, entity)
            return self._entity_cache[key]

    def exact_identities_many(
        self,
        requests: Sequence[tuple[str, frozenset[str]]],
    ) -> ExactIdentityBatch:
        normalized = _normalize_requests(
            requests,
            normalizer=lambda value: str(value or "").strip().lower(),
        )
        output: list[list[TerminologyIdentity]] = [[] for _ in normalized]
        definitions = {
            "drug_terms": (
                self.paths.drug,
                """
                SELECT lower(trim(t.term)) query_key,
                       'drug:' || lower(t.entity_type) || ':' || t.entity_id entity_id
                  FROM drug_terms t WHERE lower(trim(t.term)) IN ({placeholders})
                UNION
                SELECT lower(trim(i.canonical_en)),
                       'drug:ingredient:' || i.ingredient_id
                  FROM ingredients i WHERE lower(trim(i.canonical_en)) IN ({placeholders})
                UNION
                SELECT lower(trim(p.product_name_en)), 'drug:product:' || p.item_id
                  FROM products p WHERE lower(trim(p.product_name_en)) IN ({placeholders})
                """,
                3,
            ),
            "procedure_terms": (
                self.paths.procedure,
                """
                SELECT lower(trim(t.canonical_name_en)) query_key,
                       'procedure:' || t.term_id entity_id
                  FROM clinical_terms t WHERE lower(trim(t.canonical_name_en)) IN ({placeholders})
                UNION
                SELECT lower(trim(t.canonical_name_ko)), 'procedure:' || t.term_id
                  FROM clinical_terms t WHERE lower(trim(t.canonical_name_ko)) IN ({placeholders})
                UNION
                SELECT lower(trim(a.alias)), 'procedure:' || a.term_id
                  FROM term_aliases a WHERE lower(trim(a.alias)) IN ({placeholders})
                """,
                3,
            ),
            "anatomy_terms": (
                self.paths.anatomy,
                """
                SELECT lower(trim(t.english_name)) query_key,
                       'anatomy:' || t.term_id entity_id
                  FROM anatomical_terms t WHERE lower(trim(t.english_name)) IN ({placeholders})
                UNION
                SELECT lower(trim(t.latin_name)), 'anatomy:' || t.term_id
                  FROM anatomical_terms t WHERE lower(trim(t.latin_name)) IN ({placeholders})
                UNION
                SELECT lower(trim(t.korean_name)), 'anatomy:' || t.term_id
                  FROM anatomical_terms t WHERE lower(trim(t.korean_name)) IN ({placeholders})
                UNION
                SELECT lower(trim(a.alias)), 'anatomy:' || a.term_id
                  FROM anatomical_aliases a WHERE lower(trim(a.alias)) IN ({placeholders})
                """,
                4,
            ),
            "emergency_terms": (
                self.paths.emergency,
                """
                SELECT lower(trim(t.standard_en)) query_key,
                       'emergency:' || t.term_id entity_id
                  FROM terms t WHERE lower(trim(t.standard_en)) IN ({placeholders})
                UNION
                SELECT lower(trim(t.standard_ko)), 'emergency:' || t.term_id
                  FROM terms t WHERE lower(trim(t.standard_ko)) IN ({placeholders})
                UNION
                SELECT lower(trim(a.alias)), 'emergency:' || a.term_id
                  FROM aliases a WHERE lower(trim(a.alias)) IN ({placeholders})
                """,
                3,
            ),
            "kcd9_terms": (
                self.paths.kcd9,
                """
                SELECT lower(trim(t.en_name)) query_key, 'kcd:' || t.code entity_id
                  FROM kcd_terms t JOIN kcd_codes c USING(code)
                 WHERE c.is_complete=1 AND lower(trim(t.en_name)) IN ({placeholders})
                UNION
                SELECT lower(trim(t.ko_name)), 'kcd:' || t.code
                  FROM kcd_terms t JOIN kcd_codes c USING(code)
                 WHERE c.is_complete=1 AND lower(trim(t.ko_name)) IN ({placeholders})
                UNION
                SELECT lower(trim(c.code)), 'kcd:' || c.code
                  FROM kcd_codes c
                 WHERE c.is_complete=1 AND lower(trim(c.code)) IN ({placeholders})
                """,
                3,
            ),
        }
        statement_count = 0
        with self.request_session():
            for collection in sorted(definitions, key=_COLLECTION_ORDER.__getitem__):
                key_to_indexes = _keys_for_collection(normalized, collection)
                if not key_to_indexes:
                    continue
                keys = tuple(key_to_indexes)
                placeholders = ", ".join("?" for _ in keys)
                path, sql, repetitions = definitions[collection]
                try:
                    rows = self._rows(
                        path,
                        sql.format(placeholders=placeholders),
                        keys * repetitions,
                    )
                except (OSError, sqlite3.Error):
                    continue
                statement_count += 1
                for row in rows:
                    for index in key_to_indexes.get(
                        str(row["query_key"] or "").strip().lower(), ()
                    ):
                        output[index].append(
                            TerminologyIdentity(collection, str(row["entity_id"]))
                        )
        return ExactIdentityBatch(_deduplicate(output), statement_count)


@dataclass(slots=True)
class _PostgresSession:
    connection: Any
    depth: int = 1


class PostgresTerminologyRepository:
    """Read terminology from active versioned releases in PostgreSQL."""

    def __init__(self, database_url: str) -> None:
        cleaned_url = _normalize_database_url(database_url)
        if not cleaned_url:
            raise ValueError("PostgreSQL terminology repository requires a database URL")
        self._database_url = cleaned_url
        self._local = threading.local()
        self._entity_cache: dict[tuple[str, str], TerminologyEntity | None] = {}
        self._entity_cache_lock = threading.Lock()
        with self.request_session():
            rows = self._execute(
                """
                SELECT source_kind, source_id, version, content_hash
                  FROM clinicalnlp.source_releases
                 WHERE is_active
                   AND source_kind IN ('MEDICAL_DICTIONARY', 'KCD')
                 ORDER BY source_kind, source_id
                """,
                (),
            )
        if len(rows) < 5:
            raise RuntimeError("PostgreSQL terminology releases are not ready")
        digest = hashlib.sha256()
        for row in rows:
            digest.update("\0".join(str(value) for value in row).encode("utf-8"))
            digest.update(b"\0")
        self.version = "postgres:sha256:" + digest.hexdigest()

    @staticmethod
    def _connect(database_url: str):
        try:
            import psycopg
        except ImportError as error:
            raise RuntimeError("psycopg is required for PostgreSQL terminology") from error
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
            raise RuntimeError("terminology access requires a request session")
        with session.connection.cursor() as cursor:
            cursor.execute(sql, tuple(parameters))
            return list(cursor.fetchall())

    def _lookup_uncached(
        self,
        collection: str,
        entity_id: str,
    ) -> TerminologyEntity | None:
        if collection not in SUPPORTED_COLLECTIONS:
            return None
        with self.request_session():
            if collection == "kcd9_terms":
                if not entity_id.startswith("kcd:"):
                    return None
                rows = self._execute(
                    """
                    SELECT c.canonical_ko_name, c.canonical_en_name, 'official'
                      FROM clinicalnlp.kcd_codes c
                      JOIN clinicalnlp.source_releases r
                        ON r.release_id=c.source_release_id
                     WHERE r.is_active AND r.source_kind='KCD'
                       AND c.code=%s AND c.is_complete LIMIT 1
                    """,
                    (entity_id.split(":", 1)[1],),
                )
            else:
                rows = self._execute(
                    """
                    SELECT c.canonical_ko, c.canonical_en, c.review_status
                      FROM clinicalnlp.medical_concepts c
                      JOIN clinicalnlp.source_releases r
                        ON r.release_id=c.source_release_id
                     WHERE r.is_active AND r.source_kind='MEDICAL_DICTIONARY'
                       AND c.collection_name=%s AND c.entity_id=%s LIMIT 1
                    """,
                    (collection, entity_id),
                )
        if not rows:
            return None
        canonical_ko, canonical_en, review_status = rows[0]
        if collection == "drug_terms":
            # The historical SQLite resolver exposes verified drug rows as
            # `official` regardless of the source concept-status vocabulary.
            review_status = "official"
        return _entity_from_values(
            collection,
            entity_id,
            canonical_ko,
            canonical_en,
            review_status,
        )

    def lookup(self, collection: str, entity_id: str) -> TerminologyEntity | None:
        key = (collection, entity_id)
        with self._entity_cache_lock:
            if key in self._entity_cache:
                return self._entity_cache[key]
        entity = self._lookup_uncached(collection, entity_id)
        with self._entity_cache_lock:
            self._entity_cache.setdefault(key, entity)
            return self._entity_cache[key]

    def exact_identities_many(
        self,
        requests: Sequence[tuple[str, frozenset[str]]],
    ) -> ExactIdentityBatch:
        normalized = _normalize_requests(requests)
        output: list[list[TerminologyIdentity]] = [[] for _ in normalized]
        statement_count = 0
        with self.request_session():
            for collection in sorted(SUPPORTED_COLLECTIONS, key=_COLLECTION_ORDER.__getitem__):
                key_to_indexes = _keys_for_collection(normalized, collection)
                if not key_to_indexes:
                    continue
                keys = list(key_to_indexes)
                if collection == "kcd9_terms":
                    rows = self._execute(
                        """
                        SELECT matches.query_key, matches.entity_id FROM (
                            SELECT q.query_key,
                                   'kcd:' || c.code entity_id
                              FROM clinicalnlp.kcd_terms t
                              JOIN clinicalnlp.kcd_codes c
                                ON c.kcd_code_pk=t.kcd_code_pk
                              JOIN clinicalnlp.source_releases r
                                ON r.release_id=c.source_release_id
                              JOIN unnest(%s::text[]) q(query_key)
                                ON q.query_key=t.normalized_term
                                OR q.query_key=lower(trim(t.ko_name))
                                OR q.query_key=lower(trim(t.en_name))
                             WHERE r.is_active AND r.source_kind='KCD'
                               AND c.is_complete
                            UNION
                            SELECT q.query_key, 'kcd:' || c.code
                              FROM clinicalnlp.kcd_codes c
                              JOIN clinicalnlp.source_releases r
                                ON r.release_id=c.source_release_id
                              JOIN unnest(%s::text[]) q(query_key)
                                ON q.query_key=lower(trim(c.code))
                             WHERE r.is_active AND r.source_kind='KCD'
                               AND c.is_complete
                        ) matches
                        """,
                        (keys, keys),
                    )
                else:
                    rows = self._execute(
                        """
                        SELECT q.query_key, c.entity_id
                          FROM clinicalnlp.medical_terms t
                          JOIN clinicalnlp.medical_concepts c
                            ON c.concept_pk=t.concept_pk
                          JOIN clinicalnlp.source_releases r
                            ON r.release_id=c.source_release_id
                          JOIN unnest(%s::text[]) q(query_key)
                            ON q.query_key=t.normalized_term
                            OR q.query_key=lower(trim(t.source_text))
                         WHERE r.is_active
                           AND r.source_kind='MEDICAL_DICTIONARY'
                           AND c.collection_name=%s
                        """,
                        (keys, collection),
                    )
                statement_count += 1
                for query_key, entity_id in rows:
                    for index in key_to_indexes.get(str(query_key), ()):
                        output[index].append(
                            TerminologyIdentity(collection, str(entity_id))
                        )
        return ExactIdentityBatch(_deduplicate(output), statement_count)


class ShadowTerminologyRepository:
    """Compare PostgreSQL reads while preserving primary output exactly."""

    def __init__(
        self,
        primary: TerminologyRepository,
        secondary: TerminologyRepository,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.version = primary.version
        self.local_source_hashes = getattr(primary, "local_source_hashes", None)
        self._local = threading.local()
        self._mismatch_count = 0
        self._lock = threading.Lock()

    @property
    def mismatch_count(self) -> int:
        with self._lock:
            return self._mismatch_count

    def _record_mismatch(self, operation: str) -> None:
        with self._lock:
            self._mismatch_count += 1
        LOGGER.warning("terminology shadow mismatch operation=%s", operation)

    @contextmanager
    def request_session(self):
        with ExitStack() as stack:
            stack.enter_context(self.primary.request_session())
            try:
                stack.enter_context(self.secondary.request_session())
            except Exception:
                self._record_mismatch("session_error")
            yield self

    def lookup(self, collection: str, entity_id: str) -> TerminologyEntity | None:
        primary = self.primary.lookup(collection, entity_id)
        try:
            secondary = self.secondary.lookup(collection, entity_id)
        except Exception:
            self._record_mismatch("lookup_error")
        else:
            if primary != secondary:
                self._record_mismatch("lookup")
        return primary

    def exact_identities_many(
        self,
        requests: Sequence[tuple[str, frozenset[str]]],
    ) -> ExactIdentityBatch:
        primary = self.primary.exact_identities_many(requests)
        try:
            secondary = self.secondary.exact_identities_many(requests)
        except Exception:
            self._record_mismatch("exact_error")
        else:
            if primary.identities != secondary.identities:
                self._record_mismatch("exact")
        return primary


def create_terminology_repository(
    *,
    mode: str,
    db_root: Path,
    database_url: str,
) -> TerminologyRepository:
    normalized_mode = mode.strip().casefold()
    if normalized_mode == "sqlite":
        return SqliteTerminologyRepository(db_root)
    if normalized_mode not in {"shadow", "postgres"}:
        raise ValueError("terminology backend must be sqlite, shadow, or postgres")
    if normalized_mode == "shadow":
        sqlite_repository = SqliteTerminologyRepository(db_root)
        try:
            postgres_repository = PostgresTerminologyRepository(database_url)
        except Exception:
            LOGGER.warning("terminology shadow secondary unavailable")
            postgres_repository = _UnavailableTerminologyRepository()
        return ShadowTerminologyRepository(sqlite_repository, postgres_repository)
    postgres_repository = PostgresTerminologyRepository(database_url)
    return postgres_repository


def _normalize_database_url(value: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith("postgresql+psycopg://"):
        return "postgresql://" + cleaned.removeprefix("postgresql+psycopg://")
    return cleaned


def _normalize_requests(
    requests: Sequence[tuple[str, frozenset[str]]],
    *,
    normalizer=normalize_terminology_query,
) -> tuple[tuple[str, frozenset[str]], ...]:
    if len(requests) > 64:
        raise ValueError("terminology batch may contain at most 64 queries")
    return tuple(
        (
            normalizer(query_text),
            frozenset(collections) & SUPPORTED_COLLECTIONS,
        )
        for query_text, collections in requests
    )


class _UnavailableTerminologyRepository:
    version = "unavailable"

    @contextmanager
    def request_session(self):
        raise RuntimeError("secondary terminology repository unavailable")
        yield self

    def lookup(self, collection: str, entity_id: str) -> TerminologyEntity | None:
        raise RuntimeError("secondary terminology repository unavailable")

    def exact_identities_many(
        self,
        requests: Sequence[tuple[str, frozenset[str]]],
    ) -> ExactIdentityBatch:
        raise RuntimeError("secondary terminology repository unavailable")


def _keys_for_collection(
    requests: Sequence[tuple[str, frozenset[str]]],
    collection: str,
) -> dict[str, list[int]]:
    keys: dict[str, list[int]] = {}
    for index, (query_text, collections) in enumerate(requests):
        if query_text and collection in collections:
            keys.setdefault(query_text, []).append(index)
    return keys


def _deduplicate(
    rows: Sequence[Sequence[TerminologyIdentity]],
) -> tuple[tuple[TerminologyIdentity, ...], ...]:
    return tuple(
        tuple(dict.fromkeys(sorted(items, key=lambda item: (item.collection, item.entity_id))))
        for items in rows
    )


def _entity_from_row(
    collection: str,
    entity_id: str,
    row: sqlite3.Row | None,
) -> TerminologyEntity | None:
    if row is None:
        return None
    return _entity_from_values(
        collection,
        entity_id,
        row["canonical_ko"],
        row["canonical_en"],
        row["review_status"],
    )


def _entity_from_values(
    collection: str,
    entity_id: str,
    canonical_ko_value: object,
    canonical_en_value: object,
    review_status_value: object,
) -> TerminologyEntity | None:
    canonical_ko = str(canonical_ko_value or "").strip()
    canonical_en = str(canonical_en_value or "").strip() or None
    if not canonical_ko:
        return None
    return TerminologyEntity(
        collection=collection,
        entity_id=entity_id,
        canonical_ko=canonical_ko,
        canonical_en=canonical_en,
        review_status=str(review_status_value or "").casefold(),
    )
