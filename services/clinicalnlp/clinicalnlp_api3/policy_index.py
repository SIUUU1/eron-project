from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

from .policy_catalog import load_policy_catalog, validate_policy_catalog
from .vector_store import MedicalHashEmbedder, VECTOR_DIMENSIONS


DEFAULT_POLICY_INDEX = Path(__file__).parents[1] / "data" / "policy_vectors.sqlite"
DEFAULT_POLICY_REFERENCE_ROOT = (
    Path(__file__).parents[1]
    / "docs"
    / "ERON_Guideline_Reference_Package_v1"
)
POLICY_TABLES = (
    "policy_documents",
    "policy_chunks",
    "policy_chunks_fts",
    "policy_vectors",
)
_ARTICLE_RE = re.compile(r"(제\d+조(?:의\d+)?)")
_ARTICLE_TITLE_RE = re.compile(r"(제\d+조(?:의\d+)?\([^)]*\))")
_PARAGRAPH_RE = re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]")
_PARAGRAPH_SEARCH_RE = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]")


@dataclass(frozen=True)
class PolicyChunk:
    chunk_id: str
    source_id: str
    ordinal: int
    section: str | None
    page: int | None
    article: str | None
    text: str
    rule_ids: tuple[str, ...]
    source_path: str


class _BlockParser(HTMLParser):
    _BLOCK_TAGS = frozenset({"h1", "h2", "h3", "h4", "p", "li", "th", "td"})
    _SKIP_TAGS = frozenset({"script", "style", "nav", "footer"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self._active_tag: str | None = None
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        del attrs
        tag = tag.casefold()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self._BLOCK_TAGS and self._active_tag is None:
            self._active_tag = tag
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth or tag != self._active_tag:
            return
        text = " ".join("".join(self._parts).split())
        if text:
            self.blocks.append((tag, text))
        self._active_tag = None
        self._parts = []

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and self._active_tag is not None:
            self._parts.append(data)


def _require_sqlite_vec():
    try:
        import sqlite_vec
    except ImportError as error:  # pragma: no cover - deployment failure path
        raise RuntimeError("sqlite-vec is required for the policy index") from error
    return sqlite_vec


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    target = f"file:{path}?mode=ro" if read_only else str(path)
    connection = sqlite3.connect(target, uri=read_only)
    connection.row_factory = sqlite3.Row
    connection.enable_load_extension(True)
    _require_sqlite_vec().load(connection)
    connection.enable_load_extension(False)
    return connection


def _law_chunks(document: dict[str, Any], source: bytes) -> list[PolicyChunk]:
    parser = _BlockParser()
    parser.feed(source.decode("utf-8-sig"))
    section: str | None = None
    article: str | None = None
    paragraphs: list[str] = []
    current_parts: list[str] = []

    def flush() -> None:
        if current_parts:
            paragraphs.append("\n".join(current_parts))
            current_parts.clear()

    for tag, text in parser.blocks:
        article_match = _ARTICLE_RE.search(text)
        if article_match and "(" in text:
            flush()
            article = article_match.group(1)
            title_match = _ARTICLE_TITLE_RE.search(text)
            section = title_match.group(1) if title_match else text
            paragraph_match = _PARAGRAPH_SEARCH_RE.search(text)
            if paragraph_match:
                current_parts.append(text[paragraph_match.start() :])
            continue
        if article is not None and _PARAGRAPH_RE.match(text):
            flush()
            current_parts.append(text)
        elif article is not None and current_parts and tag == "li":
            current_parts.append(text)
    flush()

    source_id = str(document["source_id"])
    article_number = re.sub(r"\D", "", article or "unknown") or "unknown"
    return [
        PolicyChunk(
            chunk_id=f"{source_id}-a{article_number}-p{ordinal:02d}-c01",
            source_id=source_id,
            ordinal=ordinal,
            section=section,
            page=None,
            article=article,
            text=text,
            rule_ids=tuple(document.get("rule_ids") or ()),
            source_path=str(document["source_path"]),
        )
        for ordinal, text in enumerate(paragraphs, start=1)
    ]


def _pdf_section(text: str, previous: str | None) -> str | None:
    for line in text.splitlines():
        candidate = " ".join(line.split())
        if not candidate or len(candidate) > 140:
            continue
        if re.fullmatch(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}\.?", candidate):
            continue
        if re.match(
            r"^(?:[IVXⅠⅡⅢⅣⅤ]+[.)\s]|\d+(?:\.\d+)*[.)\s]|"
            r"(?:Who|Why|What|Where|When|How):)",
            candidate,
            re.IGNORECASE,
        ):
            return candidate
    return previous


def _pdf_chunks(document: dict[str, Any], source: bytes) -> list[PolicyChunk]:
    try:
        from pypdf import PdfReader
    except ImportError as error:  # pragma: no cover - deployment failure path
        raise RuntimeError("pypdf is required to index policy PDFs") from error

    reader = PdfReader(BytesIO(source))
    source_id = str(document["source_id"])
    chunks: list[PolicyChunk] = []
    section: str | None = str(document.get("title") or source_id)
    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text(extraction_mode="layout") or "").replace(
            "\r\n", "\n"
        ).replace("\r", "\n").strip()
        if not text:
            continue
        section = _pdf_section(text, section)
        chunks.append(
            PolicyChunk(
                chunk_id=f"{source_id}-p{page_number:03d}-c01",
                source_id=source_id,
                ordinal=page_number,
                section=section,
                page=page_number,
                article=None,
                text=text,
                rule_ids=tuple(document.get("rule_ids") or ()),
                source_path=str(document["source_path"]),
            )
        )
    return chunks


def _html_chunks(document: dict[str, Any], source: bytes) -> list[PolicyChunk]:
    parser = _BlockParser()
    parser.feed(source.decode("utf-8-sig"))
    source_id = str(document["source_id"])
    section = str(document.get("title") or source_id)
    parts: list[str] = []
    grouped: list[tuple[str, str]] = []

    def flush() -> None:
        if parts:
            grouped.append((section, "\n".join(parts)))
            parts.clear()

    for tag, text in parser.blocks:
        if tag in {"h1", "h2", "h3", "h4"}:
            flush()
            section = text
        else:
            parts.append(text)
    flush()

    return [
        PolicyChunk(
            chunk_id=f"{source_id}-h{ordinal:03d}-c01",
            source_id=source_id,
            ordinal=ordinal,
            section=chunk_section,
            page=None,
            article=None,
            text=text,
            rule_ids=tuple(document.get("rule_ids") or ()),
            source_path=str(document["source_path"]),
        )
        for ordinal, (chunk_section, text) in enumerate(grouped, start=1)
    ]


def _chunks_for_document(
    document: dict[str, Any],
    source: bytes,
) -> list[PolicyChunk]:
    suffix = Path(str(document["source_path"])).suffix.casefold()
    if document.get("document_type") == "OFFICIAL_LAW" and suffix in {".html", ".htm"}:
        return _law_chunks(document, source)
    if suffix == ".pdf":
        return _pdf_chunks(document, source)
    if suffix in {".html", ".htm"}:
        return _html_chunks(document, source)
    raise ValueError(f"unsupported policy source: {document['source_id']}")


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE policy_documents(
            source_id TEXT PRIMARY KEY,
            source_family_id TEXT NOT NULL,
            title TEXT NOT NULL,
            document_type TEXT NOT NULL,
            usage_scope TEXT NOT NULL,
            jurisdiction TEXT NOT NULL,
            published_at TEXT,
            snapshot_at TEXT NOT NULL,
            source_path TEXT NOT NULL,
            source_url TEXT,
            document_hash TEXT NOT NULL,
            basis_type TEXT NOT NULL,
            rule_ids TEXT NOT NULL,
            supersedes_source_id TEXT,
            is_active INTEGER NOT NULL CHECK(is_active IN (0, 1)),
            extraction_status TEXT NOT NULL,
            chunk_count INTEGER NOT NULL
        );
        CREATE TABLE policy_chunks(
            row_id INTEGER PRIMARY KEY,
            chunk_id TEXT NOT NULL UNIQUE,
            source_id TEXT NOT NULL REFERENCES policy_documents(source_id),
            ordinal INTEGER NOT NULL,
            section TEXT,
            page INTEGER,
            article TEXT,
            text TEXT NOT NULL,
            rule_ids TEXT NOT NULL,
            source_path TEXT NOT NULL,
            content_hash TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE policy_chunks_fts USING fts5(
            chunk_id UNINDEXED,
            source_id UNINDEXED,
            section,
            article,
            text,
            tokenize='unicode61'
        );
        """
    )
    connection.execute(
        f"""CREATE VIRTUAL TABLE policy_vectors USING vec0(
            embedding float[{VECTOR_DIMENSIONS}] distance_metric=cosine,
            +chunk_id text,
            +source_id text
        )"""
    )


def _official_documents(catalog: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for document in catalog["documents"]:
        if str(document.get("source_family_id", "")).startswith("S"):
            yield document


def build_policy_index(
    reference_root: Path | str,
    index_path: Path | str = DEFAULT_POLICY_INDEX,
    *,
    catalog_path: Path | str | None = None,
) -> dict[str, Any]:
    """Build an atomic policy document/chunk/FTS/vector index."""

    reference_root = Path(reference_root)
    index_path = Path(index_path)
    catalog = load_policy_catalog(catalog_path)
    contract_errors = validate_policy_catalog(catalog)
    if contract_errors:
        raise ValueError("invalid policy catalog: " + "; ".join(contract_errors))

    documents = list(_official_documents(catalog))
    active_source_ids = set((catalog.get("active_source_versions") or {}).values())
    parsed: list[tuple[dict[str, Any], list[PolicyChunk]]] = []
    for document in documents:
        source_path = reference_root / str(document["source_path"])
        source = source_path.read_bytes()
        actual_hash = "sha256:" + hashlib.sha256(source).hexdigest()
        if actual_hash != document["document_hash"]:
            raise ValueError(f"{document['source_id']}: document_hash mismatch")
        chunks = _chunks_for_document(document, source)
        if not chunks and document["source_id"] in active_source_ids:
            raise ValueError(f"{document['source_id']}: no policy chunks extracted")
        parsed.append((document, chunks))

    index_path.parent.mkdir(parents=True, exist_ok=True)
    building_path = index_path.with_name(index_path.name + ".building")
    if building_path.exists():
        building_path.unlink()
    connection = _connect(building_path)
    embedder = MedicalHashEmbedder()
    chunk_count = 0
    fingerprint = hashlib.sha256()
    try:
        _create_schema(connection)
        for document, chunks in parsed:
            connection.execute(
                """INSERT INTO policy_documents VALUES(
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                (
                    document["source_id"],
                    document["source_family_id"],
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
                    json.dumps(document["rule_ids"], ensure_ascii=False),
                    document["supersedes_source_id"],
                    int(document["source_id"] in active_source_ids),
                    "INDEXED" if chunks else "NO_EXTRACTABLE_CONTENT",
                    len(chunks),
                ),
            )
            for chunk in chunks:
                chunk_count += 1
                content_hash = "sha256:" + hashlib.sha256(
                    chunk.text.encode("utf-8")
                ).hexdigest()
                rule_ids_json = json.dumps(chunk.rule_ids, ensure_ascii=False)
                connection.execute(
                    """INSERT INTO policy_chunks VALUES(
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )""",
                    (
                        chunk_count,
                        chunk.chunk_id,
                        chunk.source_id,
                        chunk.ordinal,
                        chunk.section,
                        chunk.page,
                        chunk.article,
                        chunk.text,
                        rule_ids_json,
                        chunk.source_path,
                        content_hash,
                    ),
                )
                connection.execute(
                    """INSERT INTO policy_chunks_fts(
                        rowid, chunk_id, source_id, section, article, text
                    ) VALUES(?, ?, ?, ?, ?, ?)""",
                    (
                        chunk_count,
                        chunk.chunk_id,
                        chunk.source_id,
                        chunk.section or "",
                        chunk.article or "",
                        chunk.text,
                    ),
                )
                connection.execute(
                    """INSERT INTO policy_vectors(
                        rowid, embedding, chunk_id, source_id
                    ) VALUES(?, ?, ?, ?)""",
                    (
                        chunk_count,
                        embedder.embed(chunk.text),
                        chunk.chunk_id,
                        chunk.source_id,
                    ),
                )
                fingerprint.update(
                    f"{chunk.chunk_id}\0{content_hash}\n".encode("utf-8")
                )
        connection.commit()
    except Exception:
        connection.close()
        if building_path.exists():
            building_path.unlink()
        raise
    else:
        connection.close()

    os.replace(building_path, index_path)
    return {
        "index_path": str(index_path),
        "tables": list(POLICY_TABLES),
        "documents": len(documents),
        "active_documents": len(active_source_ids & {item["source_id"] for item in documents}),
        "active_source_ids": sorted(
            active_source_ids & {item["source_id"] for item in documents}
        ),
        "chunks": chunk_count,
        "vectors": chunk_count,
        "dimensions": VECTOR_DIMENSIONS,
        "index_fingerprint": "sha256:" + fingerprint.hexdigest(),
    }


def policy_chunks_for_source(
    index_path: Path | str,
    source_id: str,
) -> list[dict[str, Any]]:
    """Return indexed chunks for build inspection, ordered by source position."""

    connection = _connect(Path(index_path))
    try:
        rows = connection.execute(
            """SELECT chunk_id, source_id, ordinal, section, page, article,
                      text, rule_ids, source_path, content_hash
                 FROM policy_chunks
                WHERE source_id=?
                ORDER BY ordinal""",
            (source_id,),
        ).fetchall()
        return [
            {
                **dict(row),
                "rule_ids": json.loads(row["rule_ids"]),
            }
            for row in rows
        ]
    finally:
        connection.close()


def policy_documents_for_family(
    index_path: Path | str,
    source_family_id: str,
) -> list[dict[str, Any]]:
    """Return every preserved version in one source family."""

    connection = _connect(Path(index_path))
    try:
        rows = connection.execute(
            """SELECT source_id, source_family_id, title, document_hash,
                      supersedes_source_id, is_active, source_path,
                      extraction_status, chunk_count
                 FROM policy_documents
                WHERE source_family_id=?
                ORDER BY source_id""",
            (source_family_id,),
        ).fetchall()
        return [
            {
                **dict(row),
                "is_active": bool(row["is_active"]),
            }
            for row in rows
        ]
    finally:
        connection.close()


def audit_policy_index(index_path: Path | str) -> list[str]:
    """Check index structure, cardinality, and source locators."""

    connection = _connect(Path(index_path))
    errors: list[str] = []
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table in POLICY_TABLES:
            if table not in tables:
                errors.append(f"missing table: {table}")
        if errors:
            return errors

        chunk_count = connection.execute(
            "SELECT count(*) FROM policy_chunks"
        ).fetchone()[0]
        fts_count = connection.execute(
            "SELECT count(*) FROM policy_chunks_fts"
        ).fetchone()[0]
        vector_count = connection.execute(
            "SELECT count(*) FROM policy_vectors"
        ).fetchone()[0]
        if chunk_count != fts_count:
            errors.append("policy_chunks and policy_chunks_fts counts differ")
        if chunk_count != vector_count:
            errors.append("policy_chunks and policy_vectors counts differ")
        missing_locators = connection.execute(
            """SELECT count(*) FROM policy_chunks
                 WHERE source_path='' OR text='' OR content_hash=''"""
        ).fetchone()[0]
        if missing_locators:
            errors.append("policy chunks contain missing source locators")
        orphan_chunks = connection.execute(
            """SELECT count(*)
                 FROM policy_chunks c
                 LEFT JOIN policy_documents d ON d.source_id=c.source_id
                WHERE d.source_id IS NULL"""
        ).fetchone()[0]
        if orphan_chunks:
            errors.append("policy chunks contain unknown source documents")
        invalid_active_families = connection.execute(
            """SELECT count(*) FROM (
                   SELECT source_family_id
                     FROM policy_documents
                    GROUP BY source_family_id
                   HAVING sum(is_active) != 1
               )"""
        ).fetchone()[0]
        if invalid_active_families:
            errors.append("policy source families must have exactly one active version")
        active_without_chunks = connection.execute(
            """SELECT count(*) FROM policy_documents
                 WHERE is_active=1 AND chunk_count=0"""
        ).fetchone()[0]
        if active_without_chunks:
            errors.append("active policy documents must contain chunks")
        count_mismatches = connection.execute(
            """SELECT count(*) FROM policy_documents d
                 WHERE d.chunk_count != (
                     SELECT count(*) FROM policy_chunks c
                      WHERE c.source_id=d.source_id
                 )"""
        ).fetchone()[0]
        if count_mismatches:
            errors.append("policy document chunk counts are inconsistent")
    finally:
        connection.close()
    return errors

