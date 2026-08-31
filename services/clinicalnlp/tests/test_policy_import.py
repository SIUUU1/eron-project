from __future__ import annotations

from array import array
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from clinicalnlp_api3.policy_import import load_policy_export


class PolicyImportTests(unittest.TestCase):
    def test_load_policy_export_preserves_document_chunk_and_embedding_contract(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "policy.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE policy_documents(
                    source_id TEXT PRIMARY KEY, source_family_id TEXT,
                    title TEXT, document_type TEXT, usage_scope TEXT,
                    jurisdiction TEXT, published_at TEXT, snapshot_at TEXT,
                    source_path TEXT, source_url TEXT, document_hash TEXT,
                    basis_type TEXT, rule_ids TEXT, supersedes_source_id TEXT,
                    is_active INTEGER, extraction_status TEXT, chunk_count INTEGER
                );
                CREATE TABLE policy_chunks(
                    row_id INTEGER PRIMARY KEY, chunk_id TEXT, source_id TEXT,
                    ordinal INTEGER, section TEXT, page INTEGER, article TEXT,
                    text TEXT, rule_ids TEXT, source_path TEXT, content_hash TEXT
                );
                CREATE TABLE policy_vectors(
                    rowid INTEGER PRIMARY KEY, embedding BLOB,
                    chunk_id TEXT, source_id TEXT
                );
                """
            )
            document_hash = "a" * 64
            content_hash = "b" * 64
            connection.execute(
                "INSERT INTO policy_documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "S03", "S03", "가이드라인", "OFFICIAL_GUIDELINE",
                    "RUNTIME_VALIDATION", "KR", None, "2026-08-25",
                    "S03.pdf", None, f"sha256:{document_hash}",
                    "OFFICIAL_GUIDELINE", json.dumps(["G19"]), None,
                    1, "INDEXED", 1,
                ),
            )
            connection.execute(
                "INSERT INTO policy_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    1, "S03-p26-c02", "S03", 0, "검증", 26, None,
                    "AI 최종 확정 금지", json.dumps(["G19"]), "S03.pdf",
                    f"sha256:{content_hash}",
                ),
            )
            connection.execute(
                "INSERT INTO policy_vectors VALUES(?,?,?,?)",
                (
                    1,
                    array("f", [0.25] * 256).tobytes(),
                    "S03-p26-c02",
                    "S03",
                ),
            )
            connection.commit()
            connection.close()

            export = load_policy_export(path)

        self.assertEqual(len(export.documents), 1)
        self.assertEqual(export.documents[0]["document_hash"], document_hash)
        self.assertEqual(export.documents[0]["rule_ids"], ("G19",))
        self.assertEqual(len(export.chunks), 1)
        self.assertEqual(export.chunks[0]["chunk_id"], "S03-p26-c02")
        self.assertEqual(export.chunks[0]["content_hash"], content_hash)
        self.assertEqual(len(export.chunks[0]["embedding"]), 256)


if __name__ == "__main__":
    unittest.main()
