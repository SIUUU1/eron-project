"""Compare SQLite and PostgreSQL medical vector identities without PHI output."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sys

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from clinicalnlp_api3.medical_vector_contract import MEDICAL_VECTOR_COLLECTIONS
from clinicalnlp_api3.medical_vector_import import (
    _sqlite_vec_connection,
    read_vector_metadata,
)
from clinicalnlp_api3.medical_vector_repository import (
    PostgresMedicalVectorRepository,
    SqliteMedicalVectorRepository,
)


def _sample_requests(index_path: Path):
    with closing(_sqlite_vec_connection(index_path)) as connection:
        metadata = read_vector_metadata(connection)
        requests: list[tuple[str, frozenset[str]]] = []
        for collection in MEDICAL_VECTOR_COLLECTIONS:
            rows = connection.execute(
                f'''SELECT source_text FROM "{collection}"
                     WHERE length(trim(source_text)) >= 4
                     ORDER BY rowid LIMIT 3'''
            ).fetchall()
            requests.extend(
                (str(row["source_text"]), frozenset({collection}))
                for row in rows
            )
    return tuple(requests), {
        item.collection: item.source_sha256 for item in metadata
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--index",
        type=Path,
        default=Path(os.environ.get(
            "CLINICALNLP_API3_VECTOR_INDEX",
            "/runtime/vectors/api3_vectors.sqlite",
        )),
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("CLINICALNLP_DATABASE_URL", ""),
    )
    args = parser.parse_args(argv)

    requests, source_hashes = _sample_requests(args.index)
    sqlite_repository = SqliteMedicalVectorRepository(
        args.index,
        source_hashes=source_hashes,
    )
    postgres_repository = PostgresMedicalVectorRepository(args.database_url)
    sqlite_batch = sqlite_repository.search_many(requests, limit=20)
    postgres_batch = postgres_repository.search_many(requests, limit=20)

    top_identity_matches = 0
    overlap_total = 0.0
    for sqlite_values, postgres_values in zip(
        sqlite_batch.identities,
        postgres_batch.identities,
        strict=True,
    ):
        sqlite_top = (
            (sqlite_values[0].collection, sqlite_values[0].entity_id)
            if sqlite_values else None
        )
        postgres_top = (
            (postgres_values[0].collection, postgres_values[0].entity_id)
            if postgres_values else None
        )
        top_identity_matches += int(sqlite_top == postgres_top)
        sqlite_ids = {
            (item.collection, item.entity_id) for item in sqlite_values[:5]
        }
        postgres_ids = {
            (item.collection, item.entity_id) for item in postgres_values[:5]
        }
        union = sqlite_ids | postgres_ids
        overlap_total += len(sqlite_ids & postgres_ids) / len(union) if union else 1.0

    query_count = len(requests)
    result = {
        "schema_version": "medical-vector-backend-verification-v1",
        "status": "ready" if top_identity_matches == query_count else "mismatch",
        "query_count": query_count,
        "top_identity_matches": top_identity_matches,
        "mean_top5_jaccard": round(overlap_total / query_count, 6),
        "sqlite": {
            "elapsed_ms": sqlite_batch.elapsed_ms,
            "statement_count": sqlite_batch.statement_count,
        },
        "postgres": {
            "elapsed_ms": postgres_batch.elapsed_ms,
            "statement_count": postgres_batch.statement_count,
        },
    }
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
