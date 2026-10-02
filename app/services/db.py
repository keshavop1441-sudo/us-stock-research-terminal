"""Service-layer database access: repositories plus translation of low-level failures.

The research commands (``app.cli``) never import ``app.database.access``; they call service functions, which use
these helpers or open a repository themselves.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from app.database import access
from app.database.errors import DatabaseUnavailableError
from app.database.locking import writer_status
from app.database.read_repository import ReadRepository
from app.database.write_repository import WriteRepository
from app.models.status import RefreshStatus
from app.services.errors import DataUnavailableError
from app.services.settings_service import get_settings


def current_refresh() -> RefreshStatus:
    status = writer_status(get_settings().resolved_database_path)
    return RefreshStatus(active=status.active, operation=status.operation, pid=status.pid, started_at=status.started_at)


def _unavailable(exc: DatabaseUnavailableError) -> DataUnavailableError:
    refresh = current_refresh()
    if refresh.active:
        what = refresh.operation or "a write operation"
        since = f" since {refresh.started_at:%H:%M:%S} UTC" if refresh.started_at else ""
        message = f"The database is being updated ({what}{since}). Live data is unavailable until it finishes."
    else:
        message = str(exc)
    return DataUnavailableError(message, refresh)


@contextmanager
def read_access() -> Iterator[ReadRepository]:
    """Read repository for the configured database. Raises ``DataUnavailableError`` if it is busy."""
    try:
        with access.reader(get_settings().resolved_database_path) as repository:
            yield repository
    except DatabaseUnavailableError as exc:
        raise _unavailable(exc) from exc


@contextmanager
def write_access(operation: str) -> Iterator[WriteRepository]:
    """Write repository inside one transaction under the single-writer lock."""
    try:
        with access.writer(get_settings().resolved_database_path, operation) as repository:
            yield repository
    except DatabaseUnavailableError as exc:
        raise _unavailable(exc) from exc
