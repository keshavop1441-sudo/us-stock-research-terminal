"""Create the DuckDB database and schema if they do not exist. Safe to run repeatedly."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.database.connection import init_database  # noqa: E402


def main() -> int:
    path = Settings.from_env().resolved_database_path
    version = init_database(path)
    print(f"Database ready: {path} (schema v{version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
