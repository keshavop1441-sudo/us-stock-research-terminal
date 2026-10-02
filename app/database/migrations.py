"""Schema creation and versioned migrations. Runs only while the single-writer lock is held."""

import duckdb

from app.database.errors import MigrationError, SchemaVersionError
from app.database.schema import (
    DDL,
    DDL_V2_TABLES,
    DDL_V3_TABLES,
    META_TABLE,
    NOW_UTC,
    SCHEMA_VERSION,
    TIMESTAMP_DEFAULT_COLUMNS,
    V1_OBSOLETE_SEQUENCES,
    V2_RECREATED_TABLES,
    V3_COLUMNS,
)


def read_schema_version(con: duckdb.DuckDBPyConnection) -> int | None:
    """Stored schema version, or None for a database that has not been initialised."""
    has_meta = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [META_TABLE]
    ).fetchone()[0]
    if not has_meta:
        return None
    row = con.execute(f"SELECT value FROM {META_TABLE} WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else None


def _migrate_1_to_2(con: duckdb.DuckDBPyConnection) -> None:
    """v2 gives financial_facts/earnings/ownership/events explicit business keys (idempotent reloads).

    v1 had no write path for these tables, so they are empty; anything else means the database was
    modified outside the application and must not be silently dropped.

    It also switches timestamp defaults to explicit UTC. Timestamps already stored by v1 (only
    ``query_history.created_at`` could exist) were written in the session's local time and are left as is.
    """
    for table in V2_RECREATED_TABLES:
        rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if rows:
            raise MigrationError(f"Cannot migrate to schema v2: table '{table}' has {rows} rows.")
    for table in V2_RECREATED_TABLES:
        con.execute(f"DROP TABLE {table}")
    for sequence in V1_OBSOLETE_SEQUENCES:
        con.execute(f"DROP SEQUENCE IF EXISTS {sequence}")
    for table in V2_RECREATED_TABLES:
        con.execute(DDL_V2_TABLES[table])
    for table, column in TIMESTAMP_DEFAULT_COLUMNS:
        con.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT {NOW_UTC}")


def _migrate_2_to_3(con: duckdb.DuckDBPyConnection) -> None:
    """v3 (Phase 3A): provenance columns on ``sources``, ``financial_facts.frame``, ownership transaction detail,
    classification provenance on ``securities`` and the ``market_quotes`` table. Purely additive: no row is touched.

    Tables are altered in place (DuckDB allows ADD COLUMN on foreign-key-referenced tables); NULL means "not recorded".
    """
    for table, column, definition in V3_COLUMNS:
        con.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
    for ddl in DDL_V3_TABLES.values():
        con.execute(ddl)


# from-version -> function upgrading to from-version + 1
MIGRATIONS = {1: _migrate_1_to_2, 2: _migrate_2_to_3}
# A new database is created at this version (the v2 DDL) and then walked up through the same steps as an upgrade.
_DDL_VERSION = 2


def apply_schema(con: duckdb.DuckDBPyConnection) -> int:
    """Create or upgrade the schema in one transaction. Idempotent. Returns the schema version."""
    version = read_schema_version(con)
    if version is not None and version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"Database schema version {version} is newer than this application ({SCHEMA_VERSION})."
        )
    con.execute("BEGIN")
    try:
        if version is None:
            for statement in DDL:
                con.execute(statement)
            version = _DDL_VERSION
        for from_version in range(version, SCHEMA_VERSION):
            MIGRATIONS[from_version](con)
        con.execute(
            f"INSERT INTO {META_TABLE} VALUES ('schema_version', ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [str(SCHEMA_VERSION)],
        )
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return SCHEMA_VERSION
