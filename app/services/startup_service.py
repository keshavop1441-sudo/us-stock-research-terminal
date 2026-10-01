"""Application start-up: make sure the database exists and its schema is current."""

from dataclasses import dataclass

from app.database.connection import ensure_database
from app.database.errors import DatabaseUnavailableError
from app.services.settings_service import get_settings


@dataclass(frozen=True)
class StartupResult:
    ready: bool
    message: str | None = None  # shown to the user when not ready


def prepare_database() -> StartupResult:
    """Never raises: the app must start even if the database is busy (e.g. a refresh is running)."""
    try:
        ensure_database(get_settings().resolved_database_path)
    except DatabaseUnavailableError:
        return StartupResult(
            False, "The database is busy (a data refresh may be running). Data pages will report this."
        )
    except Exception as exc:  # noqa: BLE001 - corrupt file, permissions, newer schema, ...
        return StartupResult(False, f"The database could not be initialised: {type(exc).__name__}: {exc}")
    return StartupResult(True)
