"""Collects the health of every component for the Data Status and Settings pages.

Stale-data rule: figures are only ever shown as "live" when they were just read. If the database cannot
be read because a writer has it (typically a data refresh), the last successful reading of this process
is returned marked ``stale`` with its timestamp and the reason; if there is none, the state is
``unavailable``. The cache is in-memory and per process, so it is empty after an app restart.
"""

import threading
from datetime import UTC, datetime

from app.config import Settings
from app.data.openbb_client import installed_packages, runtime_check
from app.database import access
from app.database.errors import DatabaseUnavailableError
from app.models.status import DatabaseStatus, DataStatus, OpenBBStatus, ProviderStatus
from app.services.db import current_refresh
from app.services.settings_service import get_ollama_status

_last_live: DataStatus | None = None
_cache_lock = threading.Lock()


def check_ollama(settings: Settings) -> ProviderStatus:
    return get_ollama_status(settings)


def run_openbb_runtime_check() -> OpenBBStatus:
    """Import OpenBB V5 and report the providers it loaded (slow, on demand)."""
    return runtime_check()


def forget_last_reading() -> None:
    """Drop the cached reading (used by tests)."""
    global _last_live
    with _cache_lock:
        _last_live = None


def collect_data_status(settings: Settings) -> DataStatus:
    """Gather database, OpenBB (install-level only) and Ollama status. Never raises."""
    global _last_live
    path = settings.resolved_database_path
    refresh = current_refresh()
    database = DatabaseStatus(path=path, exists=path.is_file())
    openbb, ollama = installed_packages(), check_ollama(settings)

    def build(state: str, **fields) -> DataStatus:
        return DataStatus(database=database, data_state=state, refresh=refresh, openbb=openbb, ollama=ollama, **fields)

    if not database.exists:
        return build("unavailable", notice="The database file has not been created yet.")
    database.size_bytes = path.stat().st_size
    try:
        with access.reader(path) as repository:
            database.schema_version = repository.schema_version()
            database.initialized = database.schema_version is not None
            counts = dict(repository.table_counts()) if database.initialized else {}
            last_sync = repository.last_sync() if database.initialized else None
    except DatabaseUnavailableError as exc:
        reason = (
            f"A data refresh is in progress ({refresh.operation or 'write operation'})." if refresh.active else str(exc)
        )
        with _cache_lock:
            cached = _last_live
        if cached is None:
            return build("unavailable", notice=f"{reason} No earlier reading is available in this session.")
        database.initialized = cached.database.initialized
        database.schema_version = cached.database.schema_version
        return build(
            "stale",
            as_of=cached.as_of,
            notice=(
                f"{reason} Showing cached figures from {cached.as_of:%Y-%m-%d %H:%M:%S} UTC; they may be out of date."
            ),
            counts=cached.counts,
            last_sync=cached.last_sync,
        )
    except Exception as exc:  # noqa: BLE001 - e.g. corrupt file; report, don't crash
        database.error = f"{type(exc).__name__}: {exc}"
        return build("unavailable", notice=database.error)
    live = build("live", as_of=datetime.now(UTC).replace(tzinfo=None), counts=counts, last_sync=last_sync)
    if database.initialized:
        with _cache_lock:
            _last_live = live
    return live
