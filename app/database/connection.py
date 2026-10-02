"""Opening DuckDB connections and initialising the database.

Every connection is short-lived (open, work, close) and uses the same read-write configuration:
DuckDB refuses to open one file with mixed configurations inside a single process. A ``read_only`` flag would
therefore NOT be a usable security boundary here; read/write separation is done at the repository level
(see ``read_repository`` and ``write_repository``).
"""

import re
import time
from pathlib import Path

import duckdb

from app.database.errors import DatabaseLockedError
from app.database.locking import WriterLock
from app.database.migrations import apply_schema, read_schema_version
from app.database.schema import SCHEMA_VERSION

_RETRY_INTERVAL_SECONDS = 0.05

# DuckDB words "the file is held by another process" differently per platform:
#   Linux/macOS: 'Could not set lock on file "...": Conflicting lock is held in ... (PID n)'
#   Windows:     'Cannot open file "...": The process cannot access the file because it is being used by another
#                 process. File is already open in ... (PID n)'   (found by running the tests on real Windows CI)
_LOCK_CONFLICT = re.compile(
    r"could not set lock|conflicting lock|being used by another process|already open in", re.IGNORECASE
)


def is_lock_conflict(message: str) -> bool:
    """True if a DuckDB IOException message means another process has the database file open."""
    return bool(_LOCK_CONFLICT.search(message))


def open_connection(path: Path, *, wait: float = 0.0) -> duckdb.DuckDBPyConnection:
    """Open the database file, retrying for up to ``wait`` seconds if another process holds it.

    Raises ``DatabaseLockedError`` if the file stays locked. Any other error propagates unchanged.
    """
    deadline = time.monotonic() + wait
    while True:
        try:
            return duckdb.connect(str(path))
        except duckdb.IOException as exc:
            if not is_lock_conflict(str(exc)):
                raise
            if time.monotonic() >= deadline:
                raise DatabaseLockedError("The database file is in use by another process.") from exc
            time.sleep(_RETRY_INTERVAL_SECONDS)


def ensure_database(path: Path, *, lock_timeout: float = 1.0, connect_wait: float = 1.0) -> int:
    """Create/upgrade the schema if needed and return the schema version.

    Cheap when the database is already current: it only reads, takes no writer lock and writes nothing.
    Otherwise it takes the single-writer lock for the duration of the change.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = open_connection(path, wait=connect_wait)
    try:
        version = read_schema_version(con)
        if version == SCHEMA_VERSION:
            return version
    finally:
        con.close()
    with WriterLock(path, "schema initialisation", timeout=lock_timeout):
        con = open_connection(path, wait=connect_wait)
        try:
            return apply_schema(con)
        finally:
            con.close()
