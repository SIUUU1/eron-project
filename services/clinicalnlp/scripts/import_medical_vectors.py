"""CLI for importing the mounted sqlite-vec medical index into PostgreSQL."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from clinicalnlp_api3.medical_vector_import import import_medical_vectors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import versioned medical terminology vectors into pgvector."
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=Path(
            os.environ.get(
                "CLINICALNLP_API3_VECTOR_INDEX",
                "/runtime/vectors/api3_vectors.sqlite",
            )
        ),
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("CLINICALNLP_DATABASE_URL", ""),
    )
    args = parser.parse_args(argv)
    try:
        result = import_medical_vectors(
            index_path=args.index,
            database_url=args.database_url,
            progress=lambda collection, count: print(
                json.dumps(
                    {
                        "status": "collection_ready",
                        "collection": collection,
                        "vector_count": count,
                    },
                    separators=(",", ":"),
                ),
                file=sys.stderr,
                flush=True,
            ),
        )
    except Exception as error:
        print(f"[FATAL] medical vector import failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
