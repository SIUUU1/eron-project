from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import threading
from typing import Any

import psycopg


@dataclass(slots=True)
class _AliasRequestSession:
    depth: int = 1
    releases: dict[
        int | None,
        tuple[int, tuple[tuple[Any, ...], ...]],
    ] = field(default_factory=dict)


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
        self._request_local = threading.local()
        self._cache_lock = threading.Lock()
        self._cached_version = 0
        self._cached_rows: tuple[tuple[Any, ...], ...] = ()

    @contextmanager
    def request_session(self):
        """Keep one immutable alias-release snapshot for a resolver request."""
        session = getattr(self._request_local, "session", None)
        if session is None:
            session = _AliasRequestSession()
            self._request_local.session = session
        else:
            session.depth += 1
        try:
            yield self
        finally:
            session.depth -= 1
            if session.depth == 0:
                session.releases.clear()
                del self._request_local.session

    @staticmethod
    def _fetch_rows(cursor: Any, version: int) -> tuple[tuple[Any, ...], ...]:
        cursor.execute(
            """
            SELECT candidate_id, source_alias, collection_name,
                   entity_id, canonical_ko, canonical_en, entity_type
              FROM clinicalnlp.alias_release_entries
             WHERE version=%s
             ORDER BY length(source_alias) DESC, candidate_id
            """,
            (version,),
        )
        return tuple(cursor.fetchall())

    def _current_release(self) -> tuple[int, tuple[tuple[Any, ...], ...]]:
        with psycopg.connect(
            self._database_url,
            autocommit=True,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COALESCE(MAX(version), 0) "
                    "FROM clinicalnlp.alias_versions"
                )
                row = cursor.fetchone()
                selected_version = int(row[0]) if row is not None else 0
                if selected_version <= 0:
                    return 0, ()
                with self._cache_lock:
                    if selected_version == self._cached_version:
                        return selected_version, self._cached_rows
                rows = self._fetch_rows(cursor, selected_version)

        with self._cache_lock:
            if selected_version != self._cached_version:
                self._cached_version = selected_version
                self._cached_rows = rows
            return selected_version, self._cached_rows

    def _release(
        self,
        version: int | None,
    ) -> tuple[int, tuple[tuple[Any, ...], ...]]:
        session = getattr(self._request_local, "session", None)
        if session is not None and version in session.releases:
            return session.releases[version]

        if version is None:
            release = self._current_release()
        elif version <= 0:
            release = (0, ())
        else:
            with self._cache_lock:
                cached_rows = (
                    self._cached_rows
                    if version == self._cached_version
                    else None
                )
            if cached_rows is not None:
                release = (version, cached_rows)
            else:
                with psycopg.connect(
                    self._database_url,
                    autocommit=True,
                ) as connection:
                    with connection.cursor() as cursor:
                        rows = self._fetch_rows(cursor, version)
                release = (version, rows)

        if session is not None:
            session.releases[version] = release
        return release

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
        selected_version, rows = self._release(version)
        if selected_version <= 0:
            return []

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
