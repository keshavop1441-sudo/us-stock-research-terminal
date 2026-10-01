"""DuckDB connection handling.

Connections are short-lived: open, do the work, close. This keeps the database file
free for other processes (e.g. ``UPDATE_DATA.bat``) because DuckDB allows only one
read-write process at a time. Every connection uses the same (read-write) configuration:
DuckDB refuses to open one file with mixed configurations inside a single process, and
Streamlit serves all sessions from one process.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

from app.database.schema import DDL, META_TABLE, SCHEMA_VERSION


class SchemaVersionError(RuntimeError):
    """The database was created by a newer version of the application."""


@contextmanager
def connect(path: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    con = duckdb.connect(str(path))
    try:
        yield con
    finally:
        con.close()


def init_database(path: Path) -> int:
    """Create the database file and schema if needed. Idempotent. Returns the schema version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as con:
        row = None
        if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [META_TABLE]).fetchone()[
            0
        ]:
            row = con.execute(f"SELECT value FROM {META_TABLE} WHERE key = 'schema_version'").fetchone()
        if row is not None and int(row[0]) > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Database schema version {row[0]} is newer than this application ({SCHEMA_VERSION})."
            )
        for statement in DDL:
            con.execute(statement)
        con.execute(
            f"INSERT INTO {META_TABLE} VALUES ('schema_version', ?) ON CONFLICT DO NOTHING",
            [str(SCHEMA_VERSION)],
        )
    return SCHEMA_VERSION
