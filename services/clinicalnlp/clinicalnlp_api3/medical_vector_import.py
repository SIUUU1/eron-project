"""Import immutable medical terminology vectors from sqlite-vec to pgvector."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator

from .medical_vector_contract import (
    MEDICAL_VECTOR_COLLECTIONS,
    MEDICAL_VECTOR_MODEL_VERSION,
    VECTOR_DIMENSIONS,
    VECTOR_INDEX_SCHEMA_VERSION,
)
from .terminology_repository import _normalize_database_url


DICTIONARY_SOURCE_IDS = {
    "drug_terms": "drug_dictionary",
    "procedure_terms": "procedure_dictionary",
    "anatomy_terms": "anatomy_dictionary",
    "emergency_terms": "emergency_dictionary",
}
MODEL_VERSION = MEDICAL_VECTOR_MODEL_VERSION
IMPORT_VERSION = "medical-vector-pg-import-v1"


@dataclass(frozen=True, slots=True)
class VectorIndexMetadata:
    collection: str
    source_sha256: str
    schema_version: str
    dimensions: int
    row_count: int


@dataclass(frozen=True, slots=True)
class VectorReleaseDescriptor:
    source_id: str
    version: str
    content_hash: str
    metadata: dict[str, object]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_vector_metadata(metadata: VectorIndexMetadata) -> None:
    if metadata.collection not in MEDICAL_VECTOR_COLLECTIONS:
        raise ValueError(
            f"unsupported medical vector collection: {metadata.collection}"
        )
    if metadata.schema_version != VECTOR_INDEX_SCHEMA_VERSION:
        raise ValueError(
            "medical vector schema version mismatch: "
            f"{metadata.schema_version!r}"
        )
    if metadata.dimensions != VECTOR_DIMENSIONS:
        raise ValueError(
            "medical vector dimensions mismatch: "
            f"{metadata.dimensions!r}"
        )
    if metadata.row_count < 1:
        raise ValueError(
            f"medical vector collection is empty: {metadata.collection}"
        )
    if len(metadata.source_sha256) != 64 or any(
        character not in "0123456789abcdef"
        for character in metadata.source_sha256
    ):
        raise ValueError(
            f"invalid dictionary source hash: {metadata.collection}"
        )


def build_release_descriptor(
    index_path: Path,
    metadata: VectorIndexMetadata,
    *,
    index_sha256: str | None = None,
) -> VectorReleaseDescriptor:
    validate_vector_metadata(metadata)
    file_hash = index_sha256 or _sha256(Path(index_path))
    identity = hashlib.sha256()
    for value in (
        IMPORT_VERSION,
        metadata.collection,
        file_hash,
        metadata.source_sha256,
        metadata.schema_version,
        str(metadata.dimensions),
        str(metadata.row_count),
        MODEL_VERSION,
    ):
        identity.update(value.encode("utf-8"))
        identity.update(b"\0")
    return VectorReleaseDescriptor(
        source_id=f"medical_vector:{metadata.collection}",
        version=f"{metadata.schema_version}:{MODEL_VERSION}",
        content_hash=identity.hexdigest(),
        metadata={
            "collection": metadata.collection,
            "dictionary_source_sha256": metadata.source_sha256,
            "dimensions": metadata.dimensions,
            "index_file_sha256": file_hash,
            "import_version": IMPORT_VERSION,
            "model_version": MODEL_VERSION,
            "row_count": metadata.row_count,
            "schema_version": metadata.schema_version,
        },
    )


def _sqlite_vec_connection(index_path: Path) -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError as error:  # pragma: no cover - deployment failure
        raise RuntimeError("sqlite-vec is required to import medical vectors") from error
    connection = sqlite3.connect(
        f"file:{Path(index_path).resolve()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    connection.enable_load_extension(False)
    return connection


def read_vector_metadata(
    connection: sqlite3.Connection,
) -> tuple[VectorIndexMetadata, ...]:
    rows = connection.execute(
        """
        SELECT collection, source_sha256, schema_version, dimensions
          FROM vector_index_metadata
         WHERE collection IN (?, ?, ?, ?)
         ORDER BY collection
        """,
        MEDICAL_VECTOR_COLLECTIONS,
    ).fetchall()
    by_collection = {str(row["collection"]): row for row in rows}
    missing = set(MEDICAL_VECTOR_COLLECTIONS) - set(by_collection)
    if missing:
        raise ValueError(
            f"medical vector metadata is incomplete: {sorted(missing)}"
        )
    metadata: list[VectorIndexMetadata] = []
    for collection in MEDICAL_VECTOR_COLLECTIONS:
        row = by_collection[collection]
        item = VectorIndexMetadata(
            collection=collection,
            source_sha256=str(row["source_sha256"]),
            schema_version=str(row["schema_version"]),
            dimensions=int(row["dimensions"]),
            row_count=int(
                connection.execute(
                    f'SELECT count(*) FROM "{collection}"'
                ).fetchone()[0]
            ),
        )
        validate_vector_metadata(item)
        metadata.append(item)
    return tuple(metadata)


def _vector_literal(value: object, dimensions: int) -> str:
    import numpy as np

    if isinstance(value, (bytes, bytearray, memoryview)):
        vector = np.frombuffer(value, dtype=np.float32)
    else:
        vector = np.asarray(value, dtype=np.float32)
    if vector.size != dimensions:
        raise ValueError(
            f"vector row has {vector.size} dimensions; expected {dimensions}"
        )
    return "[" + ",".join(format(float(item), ".9g") for item in vector) + "]"


def _vector_rows(
    connection: sqlite3.Connection,
    metadata: VectorIndexMetadata,
) -> Iterator[tuple[str, str, str, str]]:
    cursor = connection.execute(
        f'''SELECT source_text, entity_id, embedding, payload
              FROM "{metadata.collection}"
             ORDER BY rowid'''
    )
    while rows := cursor.fetchmany(1_000):
        for row in rows:
            yield (
                str(row["source_text"]),
                str(row["entity_id"]),
                _vector_literal(row["embedding"], metadata.dimensions),
                str(row["payload"] or "{}"),
            )


def _connect_postgres(database_url: str):
    try:
        import psycopg
    except ImportError as error:  # pragma: no cover - deployment failure
        raise RuntimeError("psycopg is required to import medical vectors") from error
    cleaned = _normalize_database_url(database_url)
    if not cleaned:
        raise ValueError("PostgreSQL database URL is required")
    return psycopg.connect(cleaned)


def _import_collection(
    *,
    pg_connection: Any,
    sqlite_connection: sqlite3.Connection,
    metadata: VectorIndexMetadata,
    release: VectorReleaseDescriptor,
) -> int:
    source_id = DICTIONARY_SOURCE_IDS[metadata.collection]
    with pg_connection.transaction():
        with pg_connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT release_id, content_hash
                  FROM clinicalnlp.source_releases
                 WHERE source_kind='MEDICAL_DICTIONARY'
                   AND source_id=%s AND is_active
                """,
                (source_id,),
            )
            dictionary_release = cursor.fetchone()
            if dictionary_release is None:
                raise RuntimeError(
                    f"active dictionary release is missing: {source_id}"
                )
            if str(dictionary_release[1]) != metadata.source_sha256:
                raise RuntimeError(
                    "vector/dictionary source hash mismatch: "
                    f"{metadata.collection}"
                )

            cursor.execute(
                """
                WITH inserted AS (
                    INSERT INTO clinicalnlp.source_releases(
                        source_kind, source_id, version, content_hash,
                        is_active, metadata
                    ) VALUES ('VECTOR', %s, %s, %s, FALSE, %s::jsonb)
                    ON CONFLICT (
                        source_kind, source_id, version, content_hash
                    ) DO NOTHING
                    RETURNING release_id
                )
                SELECT release_id FROM inserted
                UNION ALL
                SELECT release_id
                  FROM clinicalnlp.source_releases
                 WHERE source_kind='VECTOR' AND source_id=%s
                   AND version=%s AND content_hash=%s
                LIMIT 1
                """,
                (
                    release.source_id,
                    release.version,
                    release.content_hash,
                    json.dumps(release.metadata, separators=(",", ":")),
                    release.source_id,
                    release.version,
                    release.content_hash,
                ),
            )
            vector_release_id = int(cursor.fetchone()[0])
            cursor.execute(
                """
                CREATE TEMP TABLE stage_medical_vectors(
                    source_text text NOT NULL,
                    entity_id text NOT NULL,
                    embedding_text text NOT NULL,
                    payload_text text NOT NULL
                ) ON COMMIT DROP
                """
            )
            with cursor.copy(
                "COPY stage_medical_vectors "
                "(source_text, entity_id, embedding_text, payload_text) "
                "FROM STDIN"
            ) as copy:
                for row in _vector_rows(sqlite_connection, metadata):
                    copy.write_row(row)

            cursor.execute("SELECT count(*) FROM stage_medical_vectors")
            staged_count = int(cursor.fetchone()[0])
            if staged_count != metadata.row_count:
                raise RuntimeError(
                    f"staged vector count mismatch: {metadata.collection}"
                )

            cursor.execute(
                """
                INSERT INTO clinicalnlp.medical_vectors(
                    vector_release_id, concept_pk, source_text,
                    embedding, model_version, payload
                )
                SELECT %s, c.concept_pk, s.source_text,
                       s.embedding_text::vector(256), %s,
                       s.payload_text::jsonb
                  FROM stage_medical_vectors s
                  JOIN clinicalnlp.medical_concepts c
                    ON c.source_release_id=%s
                   AND c.collection_name=%s
                   AND c.entity_id=s.entity_id
                ON CONFLICT (
                    vector_release_id, concept_pk, source_text
                ) DO NOTHING
                """,
                (
                    vector_release_id,
                    MODEL_VERSION,
                    int(dictionary_release[0]),
                    metadata.collection,
                ),
            )
            cursor.execute(
                """
                SELECT count(*)
                  FROM clinicalnlp.medical_vectors
                 WHERE vector_release_id=%s
                """,
                (vector_release_id,),
            )
            stored_count = int(cursor.fetchone()[0])
            if stored_count != metadata.row_count:
                raise RuntimeError(
                    "stored vector count mismatch: "
                    f"{metadata.collection} expected={metadata.row_count} "
                    f"actual={stored_count}"
                )

            cursor.execute(
                """
                UPDATE clinicalnlp.source_releases
                   SET is_active=FALSE
                 WHERE source_kind='VECTOR' AND source_id=%s
                   AND release_id<>%s
                """,
                (release.source_id, vector_release_id),
            )
            cursor.execute(
                """
                UPDATE clinicalnlp.source_releases
                   SET is_active=TRUE
                 WHERE release_id=%s
                """,
                (vector_release_id,),
            )
    return metadata.row_count


def import_medical_vectors(
    *,
    index_path: Path,
    database_url: str,
    progress: Callable[[str, int], None] | None = None,
) -> dict[str, object]:
    index_path = Path(index_path)
    if not index_path.is_file():
        raise ValueError(f"medical vector index is missing: {index_path}")
    index_hash = _sha256(index_path)
    counts: dict[str, int] = {}
    with closing(_sqlite_vec_connection(index_path)) as sqlite_connection:
        metadata_items = read_vector_metadata(sqlite_connection)
        with closing(_connect_postgres(database_url)) as pg_connection:
            for metadata in metadata_items:
                release = build_release_descriptor(
                    index_path,
                    metadata,
                    index_sha256=index_hash,
                )
                counts[metadata.collection] = _import_collection(
                    pg_connection=pg_connection,
                    sqlite_connection=sqlite_connection,
                    metadata=metadata,
                    release=release,
                )
                if progress is not None:
                    progress(metadata.collection, counts[metadata.collection])
    return {
        "status": "ready",
        "schema_version": IMPORT_VERSION,
        "collections": counts,
        "vector_count": sum(counts.values()),
    }
