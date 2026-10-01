"""Helper process for concurrency tests (not collected by pytest).

    python tests/hold_database.py <db_path> <mode>

Modes:  writer      hold the single-writer lock AND an open DuckDB connection with an open transaction
                    (what a running data refresh looks like)
        lock-only   hold only the single-writer lock
        duckdb-only hold only an open DuckDB connection (no writer lock, e.g. some other tool)

Prints READY once the resource is held, then waits until stdin is closed (or the process is killed).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import access  # noqa: E402
from app.database.connection import open_connection  # noqa: E402
from app.database.locking import WriterLock  # noqa: E402


def main() -> None:
    db_path, mode = Path(sys.argv[1]), sys.argv[2]
    if mode == "writer":
        with access.writer(db_path, "data refresh") as repository:
            repository.record_query("written by the refresh process")  # uncommitted while we hold it
            print("READY", flush=True)
            sys.stdin.read()
    elif mode == "lock-only":
        with WriterLock(db_path, "data refresh"):
            print("READY", flush=True)
            sys.stdin.read()
    elif mode == "duckdb-only":
        con = open_connection(db_path)
        print("READY", flush=True)
        sys.stdin.read()
        con.close()
    else:
        raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
