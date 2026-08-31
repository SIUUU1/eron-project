"""Import the immutable SQLite policy export into PostgreSQL."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from clinicalnlp_api3.policy_import import import_policy_index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index",
        type=Path,
        default=Path("/runtime/policy/policy_vectors.sqlite"),
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("CLINICALNLP_DATABASE_URL", ""),
    )
    args = parser.parse_args(argv)
    result = import_policy_index(args.index, args.database_url)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
