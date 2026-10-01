"""Data synchronization entry point (used by UPDATE_DATA.bat).

Phase 1 hardening: this exercises the single-writer machinery but no data loaders exist yet, so nothing is
downloaded and nothing is written to ``sources``. Missing data stays NULL / "N/A" - it is never invented.

Exit codes: 0 success, 1 failure, 2 another write/refresh is already running.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.data.openbb_client import runtime_check  # noqa: E402
from app.database import access  # noqa: E402
from app.database.connection import ensure_database  # noqa: E402
from app.database.errors import DatabaseUnavailableError  # noqa: E402
from app.database.locking import writer_status  # noqa: E402

EXIT_BUSY = 2


def main() -> int:
    path = Settings.from_env().resolved_database_path
    busy = writer_status(path)
    if busy.active:
        print(
            f"Another refresh or write is already running ({busy.operation or 'unknown'}, pid {busy.pid}). "
            "Not starting."
        )
        return EXIT_BUSY

    print("Checking OpenBB V5 (this can take a while on first run)...")
    openbb = runtime_check()
    for package in openbb.packages:
        print(f"  {package.name}: {package.version or 'NOT INSTALLED'}")
    print(f"  providers: {', '.join(openbb.providers) or 'N/A'}")
    print(f"  status:    {openbb.detail}")
    if not openbb.runtime_ok:
        return 1

    try:
        print(f"Database: {path} (schema v{ensure_database(path)})")
        with access.writer(path, "data refresh"):
            print("Write lock acquired. No data loaders are implemented yet - nothing was downloaded.")
    except DatabaseUnavailableError as exc:
        print(f"Another refresh or write is already running: {exc}")
        return EXIT_BUSY
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
