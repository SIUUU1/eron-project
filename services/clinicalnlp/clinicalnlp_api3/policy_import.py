from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

import numpy as np

from .medical_vector_contract import (
    MEDICAL_VECTOR_MODEL_VERSION,
    VECTOR_DIMENSIONS,
)


@dataclass(frozen=True, slots=True)
class PolicyExport:
    documents: tuple[dict[str, Any], ...]
    chunks: tuple[dict[str, Any], ...]
    index_hash: str


def _sha256_value(value: object, name: str) -> str:
    cleaned = str(value or "").strip().casefold()
    if cleaned.startswith("sha256:"):
        cleaned = cleaned[7:]
    if len(cleaned) != 64 or any(character not in "0123456789abcdef" for character in cleaned):
        raise ValueError(f"{name} must be a SHA-256 digest")
    return cleaned


def _json_string_tuple(value: object, name: str) -> tuple[str, ...]:
    decoded = json.loads(str(value or "[]"))
    if not isinstance(decoded, list) or not all(
        isinstance(item, str) and item for item in decoded
    ):
        raise ValueError(f"{name} must be a JSON string array")
    return tuple(decoded)


def _connect_index(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        import sqlite_vec

        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
    except (ImportError, sqlite3.Error):
        # A plain-table fixture does not require sqlite-vec. Production vec0
        # indexes fail on first query if the extension was actually required.
        pass
    return connection


def load_policy_export(index_path: Path | str) -> PolicyExport:
    """Load and validate one immutable SQLite policy export for PG import."""

    path = Path(index_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    connection = _connect_index(path)
    try:
        documents = []
        for row in connection.execute(
            "SELECT * FROM policy_documents ORDER BY source_id"
        ):
            document = dict(row)
            document["document_hash"] = _sha256_value(
                document.get("document_hash"),
                "document_hash",
            )
            document["rule_ids"] = _json_string_tuple(
                document.get("rule_ids"),
                "document rule_ids",
            )
            document["is_active"] = bool(document.get("is_active"))
            documents.append(document)

        vectors = {
            int(row[0]): row[1]
            for row in connection.execute(
                "SELECT rowid, embedding FROM policy_vectors ORDER BY rowid"
            )
        }
        chunks = []
        for row in connection.execute(
            "SELECT * FROM policy_chunks ORDER BY row_id"
        ):
            chunk = dict(row)
            row_id = int(chunk.pop("row_id"))
            blob = vectors.get(row_id)
            if not isinstance(blob, bytes):
                raise ValueError(f"missing policy vector for row {row_id}")
            embedding = np.frombuffer(blob, dtype=np.float32).copy()
            if embedding.shape != (VECTOR_DIMENSIONS,) or not np.isfinite(embedding).all():
                raise ValueError(f"invalid policy vector for row {row_id}")
            chunk["content_hash"] = _sha256_value(
                chunk.get("content_hash"),
                "chunk content_hash",
            )
            chunk["rule_ids"] = _json_string_tuple(
                chunk.get("rule_ids"),
                "chunk rule_ids",
            )
            chunk["embedding"] = tuple(float(value) for value in embedding)
            chunks.append(chunk)
        if len(vectors) != len(chunks):
            raise ValueError("policy chunk/vector counts differ")
        document_ids = {str(document["source_id"]) for document in documents}
        if any(str(chunk["source_id"]) not in document_ids for chunk in chunks):
            raise ValueError("policy chunk references an unknown document")
    finally:
        connection.close()
    return PolicyExport(
        documents=tuple(documents),
        chunks=tuple(chunks),
        index_hash=digest.hexdigest(),
    )


def _database_url(value: str) -> str:
    cleaned = str(value or "").strip().replace(
        "postgresql+psycopg://",
        "postgresql://",
        1,
    )
    if not cleaned:
        raise ValueError("PostgreSQL policy import requires a URL")
    return cleaned


def import_policy_index(
    index_path: Path | str,
    database_url: str,
) -> dict[str, Any]:
    """Import an immutable policy export without overwriting prior versions."""

    export = load_policy_export(index_path)
    chunks_by_source: dict[str, list[dict[str, Any]]] = {}
    for chunk in export.chunks:
        chunks_by_source.setdefault(str(chunk["source_id"]), []).append(chunk)

    import psycopg

    with psycopg.connect(_database_url(database_url)) as connection:
        with connection.cursor() as cursor:
            for document in export.documents:
                source_id = str(document["source_id"])
                family_id = str(document["source_family_id"])
                release_source_id = f"policy:{family_id}"
                metadata = json.dumps(
                    {
                        "source_id": source_id,
                        "source_family_id": family_id,
                        "index_sha256": export.index_hash,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                cursor.execute(
                    """
                    INSERT INTO clinicalnlp.source_releases(
                        source_kind, source_id, version, content_hash,
                        is_active, metadata
                    ) VALUES('POLICY', %s, %s, %s, FALSE, %s::jsonb)
                    ON CONFLICT(source_kind, source_id, version, content_hash)
                    DO NOTHING
                    RETURNING release_id
                    """,
                    (
                        release_source_id,
                        source_id,
                        document["document_hash"],
                        metadata,
                    ),
                )
                inserted = cursor.fetchone()
                if inserted is None:
                    cursor.execute(
                        """
                        SELECT release_id
                          FROM clinicalnlp.source_releases
                         WHERE source_kind='POLICY' AND source_id=%s
                           AND version=%s AND content_hash=%s
                        """,
                        (
                            release_source_id,
                            source_id,
                            document["document_hash"],
                        ),
                    )
                    inserted = cursor.fetchone()
                if inserted is None:
                    raise RuntimeError("policy release could not be resolved")
                release_id = int(inserted[0])

                if document["is_active"]:
                    cursor.execute(
                        """
                        UPDATE clinicalnlp.source_releases
                           SET is_active=FALSE
                         WHERE source_kind='POLICY' AND source_id=%s
                           AND release_id<>%s AND is_active
                        """,
                        (release_source_id, release_id),
                    )
                    cursor.execute(
                        """
                        UPDATE clinicalnlp.policy_documents
                           SET is_active=FALSE
                         WHERE source_family_id=%s AND document_pk NOT IN (
                             SELECT document_pk
                               FROM clinicalnlp.policy_documents
                              WHERE source_release_id=%s AND source_id=%s
                         )
                        """,
                        (family_id, release_id, source_id),
                    )
                cursor.execute(
                    """
                    UPDATE clinicalnlp.source_releases
                       SET is_active=%s
                     WHERE release_id=%s
                    """,
                    (document["is_active"], release_id),
                )
                cursor.execute(
                    """
                    INSERT INTO clinicalnlp.policy_documents(
                        source_release_id, source_id, source_family_id, title,
                        document_type, usage_scope, jurisdiction, published_at,
                        snapshot_at, source_path, source_url, document_hash,
                        basis_type, rule_ids, supersedes_source_id, is_active,
                        extraction_status, chunk_count
                    ) VALUES(
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                    )
                    ON CONFLICT(source_release_id, source_id) DO NOTHING
                    RETURNING document_pk
                    """,
                    (
                        release_id,
                        source_id,
                        family_id,
                        document["title"],
                        document["document_type"],
                        document["usage_scope"],
                        document["jurisdiction"],
                        document["published_at"],
                        document["snapshot_at"],
                        document["source_path"],
                        document["source_url"],
                        document["document_hash"],
                        document["basis_type"],
                        list(document["rule_ids"]),
                        document["supersedes_source_id"],
                        document["is_active"],
                        document["extraction_status"],
                        int(document["chunk_count"]),
                    ),
                )
                document_row = cursor.fetchone()
                if document_row is None:
                    cursor.execute(
                        """
                        SELECT document_pk, document_hash
                          FROM clinicalnlp.policy_documents
                         WHERE source_release_id=%s AND source_id=%s
                        """,
                        (release_id, source_id),
                    )
                    document_row = cursor.fetchone()
                    if document_row is None or str(document_row[1]) != document["document_hash"]:
                        raise RuntimeError("existing policy document does not match export")
                document_pk = int(document_row[0])
                cursor.execute(
                    """
                    UPDATE clinicalnlp.policy_documents
                       SET is_active=%s
                     WHERE document_pk=%s
                    """,
                    (document["is_active"], document_pk),
                )

                for chunk in chunks_by_source.get(source_id, []):
                    cursor.execute(
                        """
                        INSERT INTO clinicalnlp.policy_chunks(
                            document_pk, chunk_id, ordinal, section, page,
                            article, chunk_text, rule_ids, source_path,
                            content_hash
                        ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(chunk_id) DO NOTHING
                        RETURNING chunk_pk
                        """,
                        (
                            document_pk,
                            chunk["chunk_id"],
                            int(chunk["ordinal"]),
                            chunk["section"],
                            chunk["page"],
                            chunk["article"],
                            chunk["text"],
                            list(chunk["rule_ids"]),
                            chunk["source_path"],
                            chunk["content_hash"],
                        ),
                    )
                    chunk_row = cursor.fetchone()
                    if chunk_row is None:
                        cursor.execute(
                            """
                            SELECT chunk_pk, document_pk, content_hash
                              FROM clinicalnlp.policy_chunks
                             WHERE chunk_id=%s
                            """,
                            (chunk["chunk_id"],),
                        )
                        chunk_row = cursor.fetchone()
                        if (
                            chunk_row is None
                            or int(chunk_row[1]) != document_pk
                            or str(chunk_row[2]) != chunk["content_hash"]
                        ):
                            raise RuntimeError("existing policy chunk does not match export")
                    chunk_pk = int(chunk_row[0])
                    vector_literal = "[" + ",".join(
                        format(float(value), ".9g")
                        for value in chunk["embedding"]
                    ) + "]"
                    cursor.execute(
                        """
                        INSERT INTO clinicalnlp.policy_vectors(
                            chunk_pk, embedding, model_version
                        ) VALUES(%s, %s::vector(256), %s)
                        ON CONFLICT(chunk_pk) DO NOTHING
                        """,
                        (
                            chunk_pk,
                            vector_literal,
                            MEDICAL_VECTOR_MODEL_VERSION,
                        ),
                    )
        connection.commit()

    return {
        "status": "ready",
        "documents": len(export.documents),
        "active_documents": sum(
            int(document["is_active"]) for document in export.documents
        ),
        "chunks": len(export.chunks),
        "vectors": len(export.chunks),
        "index_sha256": export.index_hash,
    }
