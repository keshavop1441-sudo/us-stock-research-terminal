"""The two ways to reach the database: ``reader`` and ``writer``.

Concurrency model (see also ``app.database.locking``)
-----------------------------------------------------
* ``writer(...)`` = take the cross-process single-writer lock, open a connection, run everything in ONE
  transaction (commit on success, roll back on any exception), close, release the lock. At most one writer
  exists at any time, across processes. A killed writer leaves no stale lock and no half-written data.
* ``reader(...)`` takes no lock. If a writer process currently has the DuckDB file open, the read fails
  fast with ``DatabaseLockedError`` and callers report the situation; they never fall back to old data
  without saying so.
* Readers never create the database file.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.database.connection import open_connection
from app.database.errors import DatabaseUnavailableError
from app.database.locking import DEFAULT_LOCK_TIMEOUT_SECONDS, WriterLock
from app.database.read_repository import ReadRepository
from app.database.write_repository import WriteRepository

DEFAULT_READ_WAIT_SECONDS = 0.3  # ride out a millisecond-long UI write; do not hang while a refresh runs


def _require_database_file(path: Path) -> None:
    if not path.is_file():
        raise DatabaseUnavailableError(f"The database file does not exist yet: {path}")


@contextmanager
def reader(path: Path, *, wait: float = DEFAULT_READ_WAIT_SECONDS) -> Iterator[ReadRepository]:
    _require_database_file(path)
    con = open_connection(path, wait=wait)
    try:
        yield ReadRepository(con)
    finally:
        con.close()


@contextmanager
def writer(
    path: Path,
    operation: str,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    connect_wait: float = 5.0,
) -> Iterator[WriteRepository]:
    _require_database_file(path)
    with WriterLock(path, operation, timeout=lock_timeout):
        con = open_connection(path, wait=connect_wait)
        try:
            con.execute("BEGIN")
            try:
                yield WriteRepository(con)
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")
        finally:
            con.close()
