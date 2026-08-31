from __future__ import annotations

import math
import re
from typing import Any, Protocol

import psycopg

from .vector_store import MedicalHashEmbedder


_QUERY_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")
_MAX_RESULTS = 3
_CANDIDATE_LIMIT = 24
_EXCERPT_LENGTH = 600


class PolicyEvidenceRepository(Protocol):
    def retrieve(
        self,
        rule_id: str,
        query: str,
        *,
        usage_scope: str = "RUNTIME_VALIDATION",
        limit: int = _MAX_RESULTS,
    ) -> dict[str, Any]: ...


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
    value = normalized[start:end]
    return ("…" if start else "") + value + (
        "…" if end < len(normalized) else ""
    )


def _database_url(value: str) -> str:
    cleaned = str(value or "").strip().replace(
        "postgresql+psycopg://",
        "postgresql://",
        1,
    )
    if not cleaned:
        raise ValueError("PostgreSQL policy repository requires a URL")
    return cleaned


class PostgresPolicyEvidenceRepository:
    """Hybrid policy retrieval over the active PostgreSQL policy release."""

    def __init__(self, database_url: str) -> None:
        self._database_url = _database_url(database_url)
        self._embedder = MedicalHashEmbedder()

    def retrieve(
        self,
        rule_id: str,
        query: str,
        *,
        usage_scope: str = "RUNTIME_VALIDATION",
        limit: int = _MAX_RESULTS,
    ) -> dict[str, Any]:
        normalized_rule_id = str(rule_id or "").strip().upper()
        tokens = _query_tokens(query)
        if not tokens:
            return {"rule_id": normalized_rule_id, "results": []}
        result_limit = max(1, min(_MAX_RESULTS, int(limit)))
        vector = self._embedder.embed(query)
        vector_literal = "[" + ",".join(
            format(float(value), ".9g") for value in vector
        ) + "]"
        tsquery = " | ".join(tokens)
        with psycopg.connect(self._database_url, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT c.chunk_id, d.source_id, d.title, c.page, c.section,
                           c.chunk_text, d.usage_scope, d.basis_type,
                           ts_rank_cd(
                               c.search_document,
                               to_tsquery('simple', %s)
                           ) AS lexical_score,
                           1 - (v.embedding <=> %s::vector(256))
                               AS vector_similarity
                      FROM clinicalnlp.policy_chunks c
                      JOIN clinicalnlp.policy_documents d
                        ON d.document_pk=c.document_pk
                      JOIN clinicalnlp.source_releases r
                        ON r.release_id=d.source_release_id
                      LEFT JOIN clinicalnlp.policy_vectors v
                        ON v.chunk_pk=c.chunk_pk
                     WHERE r.is_active
                       AND r.source_kind='POLICY'
                       AND d.is_active
                       AND d.usage_scope=%s
                       AND %s=ANY(c.rule_ids)
                     ORDER BY c.chunk_id
                    """,
                    (tsquery, vector_literal, str(usage_scope), normalized_rule_id),
                )
                rows = list(cursor.fetchall())

        by_chunk = {str(row[0]): row for row in rows}
        lexical_ranked = sorted(
            (
                row for row in rows
                if row[8] is not None and float(row[8]) > 0.0
            ),
            key=lambda row: (-float(row[8]), str(row[0])),
        )[:_CANDIDATE_LIMIT]
        lexical_scores = {
            str(row[0]): 1.0 / rank
            for rank, row in enumerate(lexical_ranked, start=1)
        }
        vector_ranked = sorted(
            (
                row for row in rows
                if row[9] is not None and math.isfinite(float(row[9]))
            ),
            key=lambda row: (-float(row[9]), str(row[0])),
        )[:_CANDIDATE_LIMIT]
        vector_scores = {
            str(row[0]): max(0.0, min(1.0, float(row[9])))
            for row in vector_ranked
        }
        candidate_ids = set(lexical_scores) | set(vector_scores)
        ranked_ids = sorted(
            candidate_ids,
            key=lambda chunk_id: (
                -(
                    0.6 * lexical_scores.get(chunk_id, 0.0)
                    + 0.4 * vector_scores.get(chunk_id, 0.0)
                ),
                chunk_id,
            ),
        )[:result_limit]
        results = []
        for chunk_id in ranked_ids:
            row = by_chunk[chunk_id]
            score = (
                0.6 * lexical_scores.get(chunk_id, 0.0)
                + 0.4 * vector_scores.get(chunk_id, 0.0)
            )
            results.append(
                {
                    "source_id": str(row[1]),
                    "chunk_id": chunk_id,
                    "title": str(row[2]),
                    "page": row[3],
                    "section": row[4],
                    "excerpt": _excerpt(str(row[5]), tokens),
                    "retrieval_score": round(
                        max(0.0, min(1.0, score)),
                        6,
                    ),
                    "usage_scope": str(row[6]),
                    "basis_type": str(row[7]),
                }
            )
        return {"rule_id": normalized_rule_id, "results": results}
