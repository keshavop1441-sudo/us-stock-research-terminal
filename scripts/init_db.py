"""Create the DuckDB database and schema if needed (and upgrade an older schema). Safe to repeat."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.database.connection import ensure_database  # noqa: E402
from app.database.errors import DatabaseUnavailableError  # noqa: E402


def main() -> int:
    path = Settings.from_env().resolved_database_path
    try:
        version = ensure_database(path)
    except DatabaseUnavailableError as exc:
        print(f"ERROR: the database is busy ({exc}). A data refresh may be running; try again when it finishes.")
        return 2
    print(f"Database ready: {path} (schema v{version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
