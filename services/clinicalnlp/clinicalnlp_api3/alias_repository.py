from __future__ import annotations

from typing import Any

import psycopg


def _database_url(value: str) -> str:
    cleaned = str(value or "").strip().replace(
        "postgresql+psycopg://",
        "postgresql://",
        1,
    )
    if not cleaned:
        raise ValueError("PostgreSQL alias repository requires a URL")
    return cleaned


class PostgresApprovedAliasStore:
    """Read immutable approved-alias releases from PostgreSQL."""

    def __init__(self, database_url: str) -> None:
        self._database_url = _database_url(database_url)

    def current_version(self) -> int:
        with psycopg.connect(self._database_url, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COALESCE(MAX(version), 0) "
                    "FROM clinicalnlp.alias_versions"
                )
                row = cursor.fetchone()
        return int(row[0]) if row is not None else 0

    def find_approved(
        self,
        text: str,
        *,
        version: int | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(text, str) or not text:
            return []
        with psycopg.connect(self._database_url, autocommit=True) as connection:
            with connection.cursor() as cursor:
                selected_version = version
                if selected_version is None:
                    cursor.execute(
                        "SELECT COALESCE(MAX(version), 0) "
                        "FROM clinicalnlp.alias_versions"
                    )
                    row = cursor.fetchone()
                    selected_version = int(row[0]) if row is not None else 0
                if selected_version <= 0:
                    return []
                cursor.execute(
                    """
                    SELECT candidate_id, source_alias, collection_name,
                           entity_id, canonical_ko, canonical_en, entity_type
                      FROM clinicalnlp.alias_release_entries
                     WHERE version=%s
                     ORDER BY length(source_alias) DESC, candidate_id
                    """,
                    (selected_version,),
                )
                rows = list(cursor.fetchall())

        folded_text = text.casefold()
        results: list[dict[str, Any]] = []
        for row in rows:
            source_alias = str(row[1])
            folded_alias = source_alias.casefold()
            start = folded_text.find(folded_alias)
            while start >= 0:
                results.append(
                    {
                        "candidate_id": str(row[0]),
                        "source_alias": source_alias,
                        "start_char": start,
                        "end_char": start + len(source_alias),
                        "collection": str(row[2]),
                        "entity_id": str(row[3]),
                        "canonical_ko": row[4],
                        "canonical_en": row[5],
                        "entity_type": row[6],
                        "alias_db_version": selected_version,
                    }
                )
                start = folded_text.find(
                    folded_alias,
                    start + len(source_alias),
                )
        return sorted(
            results,
            key=lambda item: (
                item["start_char"],
                -(item["end_char"] - item["start_char"]),
                item["candidate_id"],
            ),
        )
