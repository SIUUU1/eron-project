"""Apply and verify the ClinicalNLP PostgreSQL schema on an existing volume."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = tuple(
    sorted((REPO / "database" / "init").glob("0[5-9]_clinicalnlp*.sql"))
)
EXPECTED_TABLE_COUNT = 15
EXPECTED_VECTOR_DIMENSIONS = 256
EXPECTED_MIGRATION = "004"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]+$")


def _dotenv_value(key: str) -> str:
    value = os.environ.get(key)
    if value:
        return value
    dotenv = REPO / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(f"[FATAL] {key} is required in .env or the environment")


def _validated(value: str, *, label: str) -> str:
    if not value or SAFE_IDENTIFIER.fullmatch(value) is None:
        raise SystemExit(f"[FATAL] invalid {label}: {value!r}")
    return value


def _psql(
    *,
    user: str,
    database: str,
    sql: str | None = None,
    input_text: str | None = None,
) -> str:
    command = [
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        user,
        "-d",
        database,
        "-v",
        "ON_ERROR_STOP=1",
        "--no-psqlrc",
    ]
    if sql is None:
        command.extend(["-q", "-f", "-"])
    else:
        command.extend(["-t", "-A", "-c", sql])
    process = subprocess.run(
        command,
        cwd=REPO,
        input=input_text,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        message = process.stderr.strip() or process.stdout.strip()
        raise SystemExit(f"[FATAL] ClinicalNLP migration failed: {message}")
    return process.stdout.strip()


def _verification_query() -> str:
    return f"""
WITH facts AS (
    SELECT
        (SELECT count(*)::integer
           FROM information_schema.tables
          WHERE table_schema = 'clinicalnlp') AS table_count,
        (SELECT regexp_replace(format_type(a.atttypid, a.atttypmod), '\\D', '', 'g')::integer
           FROM pg_attribute a
           JOIN pg_class c ON c.oid = a.attrelid
           JOIN pg_namespace n ON n.oid = c.relnamespace
          WHERE n.nspname = 'clinicalnlp'
            AND c.relname = 'medical_vectors'
            AND a.attname = 'embedding') AS vector_dimensions,
        EXISTS (
            SELECT 1
              FROM clinicalnlp.schema_migrations
             WHERE version = '{EXPECTED_MIGRATION}'
        ) AS migration_applied
)
SELECT json_build_object(
    'migration', '{EXPECTED_MIGRATION}',
    'schema', 'clinicalnlp',
    'status', CASE
        WHEN migration_applied
         AND table_count = {EXPECTED_TABLE_COUNT}
         AND vector_dimensions = {EXPECTED_VECTOR_DIMENSIONS}
        THEN 'ready'
        ELSE 'not_ready'
    END,
    'table_count', table_count,
    'vector_dimensions', vector_dimensions
)::text
FROM facts
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply the idempotent ClinicalNLP PostgreSQL migration."
    )
    parser.add_argument("--database", default=None)
    parser.add_argument("--user", default=None)
    args = parser.parse_args(argv)

    database = _validated(
        args.database or _dotenv_value("POSTGRES_DB"),
        label="database",
    )
    user = _validated(
        args.user or _dotenv_value("POSTGRES_USER"),
        label="user",
    )

    if not MIGRATIONS:
        raise SystemExit("[FATAL] no ClinicalNLP migrations found")
    for migration in MIGRATIONS:
        _psql(
            user=user,
            database=database,
            input_text=migration.read_text(encoding="utf-8"),
        )
    result = json.loads(
        _psql(
            user=user,
            database=database,
            sql=_verification_query(),
        )
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result.get("status") == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
