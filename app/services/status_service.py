"""Collects the health of every component for the Data Status and Settings pages."""

from app.agent.llm_provider import get_provider
from app.config import Settings
from app.data.openbb_client import installed_packages
from app.database import repository
from app.database.connection import connect
from app.models.status import DatabaseStatus, DataStatus, ProviderStatus


def check_ollama(settings: Settings) -> ProviderStatus:
    return get_provider(settings).check_status()


def collect_data_status(settings: Settings) -> DataStatus:
    """Gather database, OpenBB (install-level only) and Ollama status. Never raises."""
    path = settings.resolved_database_path
    database = DatabaseStatus(path=path, exists=path.is_file())
    counts: dict[str, int | None] = {}
    last_sync = None
    if database.exists:
        database.size_bytes = path.stat().st_size
        try:
            with connect(path) as con:
                database.schema_version = repository.schema_version(con)
                database.initialized = database.schema_version is not None
                if database.initialized:
                    counts = dict(repository.table_counts(con))
                    last_sync = repository.last_sync(con)
        except Exception as exc:  # noqa: BLE001 - e.g. locked or corrupt file; report, don't crash
            database.error = f"{type(exc).__name__}: {exc}"
    return DataStatus(
        database=database,
        counts=counts,
        last_sync=last_sync,
        openbb=installed_packages(),
        ollama=check_ollama(settings),
    )
