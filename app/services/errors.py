"""Errors that service functions raise for the research commands to report."""

from app.models.status import RefreshStatus


class ConfigurationError(Exception):
    """The environment/.env configuration is invalid. ``str(error)`` is safe to show."""


class DataUnavailableError(Exception):
    """The database cannot be used right now. ``str(error)`` is a user-facing explanation.

    ``refresh`` describes the writer holding the database, if one is active.
    """

    def __init__(self, message: str, refresh: RefreshStatus | None = None):
        super().__init__(message)
        self.refresh = refresh or RefreshStatus()
