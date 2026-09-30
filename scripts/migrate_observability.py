#!/usr/bin/env python3
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data.observability import migrate_observability_schema


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply additive observability schema")
    parser.add_argument(
        "--db",
        default=str(Path(__file__).resolve().parents[1] / "trading_system.db"),
    )
    args = parser.parse_args()
    db_path = Path(args.db).expanduser().resolve()
    if not db_path.is_file():
        raise SystemExit(f"OBSERVABILITY_DB_NOT_FOUND:{db_path}")
    migrate_observability_schema(str(db_path))
    print(f"OBSERVABILITY_MIGRATION_OK:{db_path}")


if __name__ == "__main__":
    main()
