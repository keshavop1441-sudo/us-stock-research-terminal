"""Opening DuckDB connections and initialising the database.

Every connection is short-lived (open, work, close) and uses the same read-write configuration:
DuckDB refuses to open one file with mixed configurations inside a single process, and Streamlit
serves all sessions from one process. A ``read_only`` flag would therefore NOT be a usable security
boundary here; read/write separation is done at the repository level (see ``read_repository`` and
``write_repository``).
"""

import time
from pathlib import Path

import duckdb

from app.database.errors import DatabaseLockedError
from app.database.locking import WriterLock
from app.database.migrations import apply_schema, read_schema_version
from app.database.schema import SCHEMA_VERSION

_RETRY_INTERVAL_SECONDS = 0.05


def open_connection(path: Path, *, wait: float = 0.0) -> duckdb.DuckDBPyConnection:
    """Open the database file, retrying for up to ``wait`` seconds if another process holds it.

    Raises ``DatabaseLockedError`` if the file stays locked. Any other error propagates unchanged.
    """
    deadline = time.monotonic() + wait
    while True:
        try:
            return duckdb.connect(str(path))
        except duckdb.IOException as exc:
            if "lock" not in str(exc).lower():
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
