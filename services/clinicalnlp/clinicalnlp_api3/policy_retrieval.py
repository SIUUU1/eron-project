from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from .policy_index import DEFAULT_POLICY_INDEX, _connect
from .vector_store import MedicalHashEmbedder


_QUERY_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")
_MAX_RESULTS = 3
_CANDIDATE_LIMIT = 24
_EXCERPT_LENGTH = 600


def _query_tokens(query: str) -> tuple[str, ...]:
    tokens: list[str] = []
    seen: set[str] = set()
    for match in _QUERY_TOKEN_RE.finditer(str(query or "")):
        token = match.group(0).casefold()
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) == 32:
            break
    return tuple(tokens)


def _fts_expression(tokens: tuple[str, ...]) -> str:
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _excerpt(text: str, tokens: tuple[str, ...]) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= _EXCERPT_LENGTH:
        return normalized

    lowered = normalized.casefold()
    positions = [lowered.find(token) for token in tokens]
    positions = [position for position in positions if position >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - 120)
    end = min(len(normalized), start + _EXCERPT_LENGTH)
    start = max(0, end - _EXCERPT_LENGTH)
    excerpt = normalized[start:end]
    return ("…" if start else "") + excerpt + ("…" if end < len(normalized) else "")


def _eligible_chunks(
    connection: sqlite3.Connection,
    rule_id: str,
    usage_scope: str,
) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT c.row_id, c.chunk_id, c.source_id, c.section, c.page,
                  c.text, d.title, d.usage_scope, d.basis_type
             FROM policy_chunks c
             JOIN policy_documents d ON d.source_id=c.source_id
            WHERE d.is_active=1
              AND d.usage_scope=?
              AND EXISTS(
                  SELECT 1 FROM json_each(c.rule_ids) WHERE value=?
              )""",
        (usage_scope, rule_id),
    ).fetchall()


def _fts_ranks(
    connection: sqlite3.Connection,
    row_ids: tuple[int, ...],
    tokens: tuple[str, ...],
) -> dict[int, float]:
    if not row_ids or not tokens:
        return {}
    placeholders = ",".join("?" for _ in row_ids)
    rows = connection.execute(
        f"""SELECT rowid
              FROM policy_chunks_fts
             WHERE policy_chunks_fts MATCH ?
               AND rowid IN ({placeholders})
             ORDER BY bm25(policy_chunks_fts, 0.0, 0.0, 2.0, 2.0, 1.0)
             LIMIT ?""",
        (_fts_expression(tokens), *row_ids, _CANDIDATE_LIMIT),
    ).fetchall()
    return {
        int(row["rowid"]): 1.0 / rank
        for rank, row in enumerate(rows, start=1)
    }


def _vector_similarities(
    connection: sqlite3.Connection,
    row_ids: tuple[int, ...],
    query: str,
) -> dict[int, float]:
    if not row_ids or not str(query or "").strip():
        return {}
    placeholders = ",".join("?" for _ in row_ids)
    query_vector = MedicalHashEmbedder().embed(query)
    rows = connection.execute(
        f"""SELECT rowid, vec_distance_cosine(embedding, ?) AS distance
              FROM policy_vectors
             WHERE rowid IN ({placeholders})
             ORDER BY distance
             LIMIT ?""",
        (query_vector, *row_ids, _CANDIDATE_LIMIT),
    ).fetchall()
    return {
        int(row["rowid"]): max(0.0, min(1.0, 1.0 - float(row["distance"])))
        for row in rows
    }


def _search_policy_index(
    index_path: Path,
    rule_id: str,
    query: str,
    usage_scope: str,
    limit: int,
) -> list[dict[str, Any]]:
    tokens = _query_tokens(query)
    if not tokens:
        return []

    connection = _connect(index_path, read_only=True)
    try:
        eligible = _eligible_chunks(connection, rule_id, usage_scope)
        if not eligible:
            return []
        by_row_id = {int(row["row_id"]): row for row in eligible}
        row_ids = tuple(by_row_id)
        fts_scores = _fts_ranks(connection, row_ids, tokens)
        vector_scores = _vector_similarities(connection, row_ids, query)
        candidate_ids = set(fts_scores) | set(vector_scores)
        ranked = sorted(
            candidate_ids,
            key=lambda row_id: (
                -(0.6 * fts_scores.get(row_id, 0.0)
                  + 0.4 * vector_scores.get(row_id, 0.0)),
                by_row_id[row_id]["chunk_id"],
            ),
        )[:limit]

        results: list[dict[str, Any]] = []
        for row_id in ranked:
            row = by_row_id[row_id]
            score = 0.6 * fts_scores.get(row_id, 0.0) + 0.4 * vector_scores.get(
                row_id, 0.0
            )
            results.append(
                {
                    "source_id": row["source_id"],
                    "chunk_id": row["chunk_id"],
                    "title": row["title"],
                    "page": row["page"],
                    "section": row["section"],
                    "excerpt": _excerpt(row["text"], tokens),
                    "retrieval_score": round(max(0.0, min(1.0, score)), 6),
                    "usage_scope": row["usage_scope"],
                    "basis_type": row["basis_type"],
                }
            )
        return results
    finally:
        connection.close()


def retrieve_policy_evidence(
    rule_id: str,
    query: str,
    *,
    usage_scope: str = "RUNTIME_VALIDATION",
    limit: int = _MAX_RESULTS,
    index_path: Path | str = DEFAULT_POLICY_INDEX,
) -> dict[str, Any]:
    """Return policy evidence; an unavailable index degrades to no evidence."""

    normalized_rule_id = str(rule_id or "").strip().upper()
    result_limit = max(1, min(_MAX_RESULTS, int(limit)))
    path = Path(index_path)
    if not path.is_file():
        return {"rule_id": normalized_rule_id, "results": []}
    try:
        results = _search_policy_index(
            path,
            normalized_rule_id,
            query,
            str(usage_scope),
            result_limit,
        )
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        results = []
    return {"rule_id": normalized_rule_id, "results": results}

