"""Cross-process single-writer lock for the DuckDB database.

Ownership model
---------------
* Exactly one writer at a time, across processes. The writer is either the data-refresh process
  (long-lived: an ingestion run) or a short administrative write (milliseconds).
* The lock is an OS-level advisory file lock (``<db>.writer.lock``, via ``filelock``: ``fcntl.flock``
  on POSIX, ``msvcrt.locking`` on Windows). The operating system releases it when the holding process
  exits for ANY reason, including a crash or kill, so there is no stale-lock cleanup to get wrong.
* While holding the lock the writer also publishes who it is in ``<db>.writer.json`` (informational
  only; the lock itself is the source of truth). A leftover file from a crashed process is harmless.
* Readers never take the lock. They simply fail to open the DuckDB file while a writer process has it
  open, and the application reports that state instead of showing stale data as if it were live.
"""

import json
import os
import socket
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from filelock import FileLock, Timeout
from pydantic import BaseModel

from app.database.errors import WriterBusyError

# Named tuple, not `except OSError, ValueError:` - that PEP 758 form is 3.14-only and `ruff format` (py314) would
# rewrite parenthesised tuples into it, which breaks the 3.13 interpreters some contributors still use.
_INFO_ERRORS = (OSError, ValueError)
DEFAULT_LOCK_TIMEOUT_SECONDS = 1.0
PROBE_TIMEOUT_SECONDS = 0.15  # long enough to ride out another probe or a millisecond-long write


def lock_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".writer.lock")


def info_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".writer.json")


class WriterStatus(BaseModel):
    active: bool
    operation: str | None = None
    pid: int | None = None
    host: str | None = None
    started_at: datetime | None = None


class WriterLock:
    """Context manager that holds the single-writer lock for ``db_path``."""

    def __init__(self, db_path: Path, operation: str, *, timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS):
        self._db_path = db_path
        self._operation = operation
        self._timeout = timeout
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(lock_path(db_path)))

    def acquire(self) -> None:
        try:
            self._lock.acquire(timeout=self._timeout)
        except Timeout:
            holder = writer_status(self._db_path)
            who = f" ({holder.operation}, pid {holder.pid})" if holder.operation else ""
            raise WriterBusyError(f"Another writer is active{who}.") from None
        info = {
            "operation": self._operation,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": datetime.now(UTC).isoformat(),
        }
        with suppress(OSError):  # informational only
            info_path(self._db_path).write_text(json.dumps(info), encoding="utf-8")

    def release(self) -> None:
        if not self._lock.is_locked:
            return
        with suppress(OSError):
            info_path(self._db_path).unlink(missing_ok=True)
        self._lock.release()

    def __enter__(self) -> "WriterLock":
        self.acquire()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()


def writer_status(db_path: Path) -> WriterStatus:
    """Is some writer active right now? Probes the lock; never blocks for long and never raises."""
    probe = FileLock(str(lock_path(db_path)))
    try:
        probe.acquire(timeout=PROBE_TIMEOUT_SECONDS)
    except Timeout:
        return _read_info(db_path)
    except OSError:  # e.g. the data directory does not exist yet: nobody can be writing
        return WriterStatus(active=False)
    probe.release()
    return WriterStatus(active=False)


def _read_info(db_path: Path) -> WriterStatus:
    try:
        info = json.loads(info_path(db_path).read_text(encoding="utf-8"))
        return WriterStatus(
            active=True,
            operation=info.get("operation"),
            pid=info.get("pid"),
            host=info.get("host"),
            started_at=info.get("started_at"),
        )
    except _INFO_ERRORS:  # lock held but info missing/unreadable (e.g. just acquired)
        return WriterStatus(active=True)
