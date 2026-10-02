"""Database-layer exceptions."""


class DatabaseUnavailableError(RuntimeError):
    """The database cannot be used right now (but may be fine later). Safe to show to users."""


class DatabaseLockedError(DatabaseUnavailableError):
    """The DuckDB file is held open by another process (typically a data refresh)."""


class WriterBusyError(DatabaseUnavailableError):
    """Another process or thread holds the single-writer lock."""


class SchemaVersionError(RuntimeError):
    """The database schema is newer than this application understands."""


class MigrationError(RuntimeError):
    """A schema migration could not be applied safely."""
