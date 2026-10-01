"""Data synchronization entry point (used by UPDATE_DATA.bat).

Foundation milestone: this prepares the database and verifies OpenBB V5 is usable, but
no data loaders exist yet, so nothing is downloaded and nothing is written to ``sources``.
Missing data stays NULL / "N/A" - it is never invented.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.data.openbb_client import runtime_check  # noqa: E402
from app.database.connection import init_database  # noqa: E402


def main() -> int:
    settings = Settings.from_env()
    path = settings.resolved_database_path
    print(f"Database: {path} (schema v{init_database(path)})")

    print("Checking OpenBB V5 (this can take a while on first run)...")
    openbb = runtime_check()
    for package in openbb.packages:
        print(f"  {package.name}: {package.version or 'NOT INSTALLED'}")
    print(f"  providers: {', '.join(openbb.providers) or 'N/A'}")
    print(f"  routers:   {', '.join(openbb.routers) or 'N/A'}")
    print(f"  status:    {openbb.detail}")
    if not openbb.runtime_ok:
        return 1

    print("\nNo data loaders are implemented yet - nothing was downloaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
