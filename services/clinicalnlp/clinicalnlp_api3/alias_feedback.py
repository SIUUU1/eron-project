from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ALIAS_DB = Path(__file__).parents[1] / "data" / "alias_feedback.sqlite"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized(value: str) -> str:
    return " ".join(value.split()).casefold()


class VersionedAliasStore:
    """Minimal, append-only approval store; never stores visits or dialogue."""

    def __init__(self, path: Path = DEFAULT_ALIAS_DB, *, confirmation_threshold: int = 2):
        if confirmation_threshold < 2:
            raise ValueError("confirmation_threshold must be at least 2")
        self.path = Path(path)
        self.confirmation_threshold = confirmation_threshold
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS alias_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS alias_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    source_alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    collection_name TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    canonical_ko TEXT,
                    canonical_en TEXT,
                    entity_type TEXT,
                    source_entity_type TEXT,
                    status TEXT NOT NULL CHECK(status IN ('PENDING', 'APPROVED', 'REJECTED')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    promoted_version INTEGER
                );

                CREATE TABLE IF NOT EXISTS alias_confirmations (
                    candidate_id TEXT NOT NULL REFERENCES alias_candidates(candidate_id),
                    actor_hash TEXT NOT NULL,
                    identity_verified INTEGER NOT NULL CHECK(identity_verified IN (0, 1)),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(candidate_id, actor_hash)
                );

                CREATE TABLE IF NOT EXISTS alias_versions (
                    version INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    promotion_reason TEXT NOT NULL,
                    actor_hash TEXT,
                    manifest_hash TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS alias_release_entries (
                    version INTEGER NOT NULL REFERENCES alias_versions(version),
                    candidate_id TEXT NOT NULL,
                    source_alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    collection_name TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    canonical_ko TEXT,
                    canonical_en TEXT,
                    entity_type TEXT,
                    source_entity_type TEXT,
                    PRIMARY KEY(version, candidate_id)
                );
                """
            )
            if connection.execute(
                "SELECT 1 FROM alias_metadata WHERE key = 'actor_hash_salt'"
            ).fetchone() is None:
                connection.execute(
                    "INSERT INTO alias_metadata(key, value) VALUES('actor_hash_salt', ?)",
                    (secrets.token_hex(32),),
                )
            connection.commit()

    @staticmethod
    def _candidate_id(
        source_alias: str,
        collection: str,
        entity_id: str,
        entity_type: str | None,
    ) -> str:
        material = "\x1f".join(
            (_normalized(source_alias), collection, entity_id, entity_type or "")
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _require_term(value: str, name: str, *, limit: int = 160) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        cleaned = " ".join(value.split())
        if not cleaned or len(cleaned) > limit:
            raise ValueError(f"{name} must contain 1-{limit} characters")
        return cleaned

    def _actor_hash(self, connection: sqlite3.Connection, actor_ref: str) -> str:
        actor_ref = self._require_term(actor_ref, "actor_ref")
        salt = connection.execute(
            "SELECT value FROM alias_metadata WHERE key = 'actor_hash_salt'"
        ).fetchone()[0]
        return hmac.new(
            bytes.fromhex(salt), actor_ref.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _verified_count(self, connection: sqlite3.Connection, candidate_id: str) -> int:
        return int(
            connection.execute(
                """
                SELECT COUNT(*) FROM alias_confirmations
                WHERE candidate_id = ? AND identity_verified = 1
                """,
                (candidate_id,),
            ).fetchone()[0]
        )

    @staticmethod
    def _eligible_for_automatic_promotion(row: sqlite3.Row) -> bool:
        if row["collection_name"] != "drug_terms":
            return True
        source_type = str(row["source_entity_type"] or "").casefold()
        entity_type = str(row["entity_type"] or "").casefold()
        return source_type in {"ingredient", "product"} and source_type == entity_type

    def _promote(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        *,
        promotion_reason: str,
        actor_hash: str | None,
    ) -> int:
        now = _now()
        connection.execute(
            """
            UPDATE alias_candidates
            SET status = 'APPROVED', updated_at = ?
            WHERE candidate_id = ? AND status = 'PENDING'
            """,
            (now, candidate_id),
        )
        version = int(
            connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM alias_versions"
            ).fetchone()[0]
        )
        approved_ids = [
            row[0]
            for row in connection.execute(
                "SELECT candidate_id FROM alias_candidates WHERE status = 'APPROVED' ORDER BY candidate_id"
            )
        ]
        manifest_hash = hashlib.sha256("\n".join(approved_ids).encode("ascii")).hexdigest()
        connection.execute(
            """
            INSERT INTO alias_versions(
                version, created_at, promotion_reason, actor_hash, manifest_hash
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (version, now, promotion_reason, actor_hash, manifest_hash),
        )
        connection.execute(
            """
            INSERT INTO alias_release_entries(
                version, candidate_id, source_alias, normalized_alias,
                collection_name, entity_id, canonical_ko, canonical_en,
                entity_type, source_entity_type
            )
            SELECT ?, candidate_id, source_alias, normalized_alias,
                   collection_name, entity_id, canonical_ko, canonical_en,
                   entity_type, source_entity_type
            FROM alias_candidates
            WHERE status = 'APPROVED'
            ORDER BY candidate_id
            """,
            (version,),
        )
        connection.execute(
            "UPDATE alias_candidates SET promoted_version = ? WHERE candidate_id = ?",
            (version, candidate_id),
        )
        return version

    def submit_selection(
        self,
        *,
        source_alias: str,
        collection: str,
        entity_id: str,
        canonical_ko: str | None,
        canonical_en: str | None,
        entity_type: str | None,
        source_entity_type: str | None,
        actor_ref: str,
        identity_verified: bool,
        direct_entry: bool,
    ) -> dict[str, Any]:
        if direct_entry:
            return {
                "stored": False,
                "reason": "DIRECT_ENTRY_CURRENT_RECORD_ONLY",
            }
        source_alias = self._require_term(source_alias, "source_alias")
        collection = self._require_term(collection, "collection", limit=80)
        entity_id = self._require_term(entity_id, "entity_id", limit=240)
        if collection == "drug_terms":
            source_type = str(source_entity_type or "").casefold()
            target_type = str(entity_type or "").casefold()
            if (
                source_type in {"ingredient", "product"}
                and target_type in {"ingredient", "product"}
                and source_type != target_type
            ):
                return {"stored": False, "reason": "DRUG_NAMING_LEVEL_MISMATCH"}
        candidate_id = self._candidate_id(
            source_alias, collection, entity_id, entity_type
        )
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO alias_candidates(
                    candidate_id, source_alias, normalized_alias, collection_name,
                    entity_id, canonical_ko, canonical_en, entity_type,
                    source_entity_type, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
                """,
                (
                    candidate_id,
                    source_alias,
                    _normalized(source_alias),
                    collection,
                    entity_id,
                    canonical_ko,
                    canonical_en,
                    entity_type,
                    source_entity_type,
                    now,
                    now,
                ),
            )
            actor_hash = self._actor_hash(connection, actor_ref)
            connection.execute(
                """
                INSERT INTO alias_confirmations(
                    candidate_id, actor_hash, identity_verified, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(candidate_id, actor_hash) DO UPDATE SET
                    identity_verified = MAX(
                        alias_confirmations.identity_verified,
                        excluded.identity_verified
                    )
                """,
                (candidate_id, actor_hash, int(bool(identity_verified)), now),
            )
            result = self._status(connection, candidate_id)
            if (
                result["status"] == "PENDING"
                and result["verified_confirmations"] >= self.confirmation_threshold
            ):
                row = connection.execute(
                    "SELECT * FROM alias_candidates WHERE candidate_id = ?",
                    (candidate_id,),
                ).fetchone()
                if self._eligible_for_automatic_promotion(row):
                    version = self._promote(
                        connection,
                        candidate_id,
                        promotion_reason="DISTINCT_VERIFIED_CLINICIANS",
                        actor_hash=None,
                    )
                    result.update(status="APPROVED", alias_db_version=version)
            connection.commit()
        return {"stored": True, "candidate_id": candidate_id, **result}

    def _status(
        self, connection: sqlite3.Connection, candidate_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            "SELECT status, promoted_version FROM alias_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise ValueError("unknown alias candidate")
        result: dict[str, Any] = {
            "status": row["status"],
            "verified_confirmations": self._verified_count(connection, candidate_id),
        }
        if row["promoted_version"] is not None:
            result["alias_db_version"] = row["promoted_version"]
        return result

    def confirm_selection(
        self,
        candidate_id: str,
        *,
        actor_ref: str,
        identity_verified: bool,
    ) -> dict[str, Any]:
        now = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM alias_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone() is None:
                raise ValueError("unknown alias candidate")
            actor_hash = self._actor_hash(connection, actor_ref)
            connection.execute(
                """
                INSERT INTO alias_confirmations(
                    candidate_id, actor_hash, identity_verified, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(candidate_id, actor_hash) DO UPDATE SET
                    identity_verified = MAX(
                        alias_confirmations.identity_verified,
                        excluded.identity_verified
                    )
                """,
                (candidate_id, actor_hash, int(bool(identity_verified)), now),
            )
            result = self._status(connection, candidate_id)
            if (
                result["status"] == "PENDING"
                and result["verified_confirmations"] >= self.confirmation_threshold
            ):
                row = connection.execute(
                    "SELECT * FROM alias_candidates WHERE candidate_id = ?",
                    (candidate_id,),
                ).fetchone()
                if self._eligible_for_automatic_promotion(row):
                    version = self._promote(
                        connection,
                        candidate_id,
                        promotion_reason="DISTINCT_VERIFIED_CLINICIANS",
                        actor_hash=None,
                    )
                    result.update(status="APPROVED", alias_db_version=version)
            connection.commit()
        return {"candidate_id": candidate_id, **result}

    def review_candidate(
        self,
        candidate_id: str,
        *,
        decision: str,
        actor_ref: str,
        actor_role: str,
        identity_verified: bool,
    ) -> dict[str, Any]:
        if actor_role != "admin" or not identity_verified:
            raise PermissionError("verified administrator identity is required")
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM alias_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown alias candidate")
            if row["status"] != "PENDING":
                raise ValueError("only pending alias candidates can be reviewed")
            actor_hash = self._actor_hash(connection, actor_ref)
            if decision == "approve":
                version = self._promote(
                    connection,
                    candidate_id,
                    promotion_reason="VERIFIED_ADMIN_APPROVAL",
                    actor_hash=actor_hash,
                )
                result = {
                    "candidate_id": candidate_id,
                    "status": "APPROVED",
                    "verified_confirmations": self._verified_count(
                        connection, candidate_id
                    ),
                    "alias_db_version": version,
                }
            else:
                connection.execute(
                    """
                    UPDATE alias_candidates
                    SET status = 'REJECTED', updated_at = ?
                    WHERE candidate_id = ?
                    """,
                    (_now(), candidate_id),
                )
                result = {
                    "candidate_id": candidate_id,
                    "status": "REJECTED",
                    "verified_confirmations": self._verified_count(
                        connection, candidate_id
                    ),
                }
            connection.commit()
        return result

    def current_version(self) -> int:
        with closing(self._connect()) as connection:
            return int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM alias_versions"
                ).fetchone()[0]
            )

    def find_approved(
        self, text: str, *, version: int | None = None
    ) -> list[dict[str, Any]]:
        if not isinstance(text, str) or not text:
            return []
        with closing(self._connect()) as connection:
            selected_version = self.current_version() if version is None else version
            if selected_version <= 0:
                return []
            rows = connection.execute(
                """
                SELECT * FROM alias_release_entries
                WHERE version = ?
                ORDER BY length(source_alias) DESC, candidate_id
                """,
                (selected_version,),
            ).fetchall()
        folded_text = text.casefold()
        results: list[dict[str, Any]] = []
        for row in rows:
            start = folded_text.find(row["source_alias"].casefold())
            while start >= 0:
                results.append(
                    {
                        "candidate_id": row["candidate_id"],
                        "source_alias": row["source_alias"],
                        "start_char": start,
                        "end_char": start + len(row["source_alias"]),
                        "collection": row["collection_name"],
                        "entity_id": row["entity_id"],
                        "canonical_ko": row["canonical_ko"],
                        "canonical_en": row["canonical_en"],
                        "entity_type": row["entity_type"],
                        "alias_db_version": selected_version,
                    }
                )
                start = folded_text.find(
                    row["source_alias"].casefold(), start + len(row["source_alias"])
                )
        return results


class VersionedApprovedAliasRetriever:
    def __init__(self, base_retriever: Any, alias_store: VersionedAliasStore):
        self.base_retriever = base_retriever
        self.alias_store = alias_store

    @property
    def alias_db_version(self) -> int:
        try:
            return self.alias_store.current_version()
        except (OSError, sqlite3.Error):
            return 0

    def retrieve(self, *, raw_text: str, context: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = list(
            self.base_retriever.retrieve(raw_text=raw_text, context=context)
        )
        try:
            approved_aliases = self.alias_store.find_approved(raw_text)
        except (OSError, sqlite3.Error):
            approved_aliases = []
        for alias in approved_aliases:
            candidates.append(
                {
                    "collection": alias["collection"],
                    "entity_id": alias["entity_id"],
                    "canonical_ko": alias["canonical_ko"],
                    "canonical_en": alias["canonical_en"],
                    "entity_type": alias["entity_type"],
                    "source_text": raw_text[
                        alias["start_char"] : alias["end_char"]
                    ],
                    "start_char": alias["start_char"],
                    "end_char": alias["end_char"],
                    "match_type": "stt_alias_exact",
                    "review_status": "approved",
                    "retrieval_score": 1.0,
                    "alias_db_version": alias["alias_db_version"],
                }
            )
        return candidates

