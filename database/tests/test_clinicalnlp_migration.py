from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import unittest
import uuid


REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = tuple(
    sorted((REPO / "database" / "init").glob("0[5-9]_clinicalnlp*.sql"))
)
RUNNER = REPO / "database" / "scripts" / "apply_clinicalnlp_schema.py"


def _dotenv_value(key: str) -> str:
    value = os.environ.get(key)
    if value:
        return value
    dotenv = REPO / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(f"{key} is required")


class ClinicalNlpMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pg_user = _dotenv_value("POSTGRES_USER")
        cls.database = f"eron_clinicalnlp_test_{uuid.uuid4().hex[:12]}"
        cls._postgres("createdb", "-U", cls.pg_user, cls.database)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._postgres("dropdb", "-U", cls.pg_user, "--if-exists", cls.database)

    @classmethod
    def _postgres(cls, *arguments: str, input_text: str | None = None) -> str:
        process = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                *arguments,
            ],
            cwd=REPO,
            input=input_text,
            capture_output=True,
            text=True,
        )
        if process.returncode != 0:
            raise AssertionError(process.stderr.strip() or process.stdout.strip())
        return process.stdout.strip()

    @classmethod
    def _query(cls, sql: str) -> str:
        return cls._postgres(
            "psql",
            "-U",
            cls.pg_user,
            "-d",
            cls.database,
            "-v",
            "ON_ERROR_STOP=1",
            "--no-psqlrc",
            "-t",
            "-A",
            "-c",
            sql,
        )

    def test_migration_is_idempotent_and_creates_the_pgvector_contract(self) -> None:
        for _ in range(2):
            for migration in MIGRATIONS:
                self._postgres(
                    "psql",
                    "-U",
                    self.pg_user,
                    "-d",
                    self.database,
                    "-v",
                    "ON_ERROR_STOP=1",
                    "--no-psqlrc",
                    "-f",
                    "-",
                    input_text=migration.read_text(encoding="utf-8"),
                )

        expected_tables = {
            "alias_candidates",
            "alias_confirmations",
            "alias_metadata",
            "alias_release_entries",
            "alias_versions",
            "kcd_codes",
            "kcd_terms",
            "medical_concepts",
            "medical_terms",
            "medical_vectors",
            "policy_chunks",
            "policy_documents",
            "policy_vectors",
            "schema_migrations",
            "source_releases",
        }
        actual_tables = set(
            self._query(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'clinicalnlp' ORDER BY table_name"
            ).splitlines()
        )

        self.assertEqual(actual_tables, expected_tables)
        self.assertEqual(
            self._query(
                "SELECT format_type(a.atttypid, a.atttypmod) "
                "FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'clinicalnlp' "
                "AND c.relname = 'medical_vectors' "
                "AND a.attname = 'embedding'"
            ),
            "vector(256)",
        )
        self.assertEqual(
            self._query(
                "SELECT count(*) FROM clinicalnlp.schema_migrations "
                "WHERE version IN ('001', '002')"
            ),
            "2",
        )

    def test_runner_applies_and_verifies_an_existing_database(self) -> None:
        process = subprocess.run(
            [
                "python3",
                str(RUNNER),
                "--database",
                self.database,
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(
            json.loads(process.stdout),
            {
                "migration": "002",
                "schema": "clinicalnlp",
                "status": "ready",
                "table_count": 15,
                "vector_dimensions": 256,
            },
        )


if __name__ == "__main__":
    unittest.main()
