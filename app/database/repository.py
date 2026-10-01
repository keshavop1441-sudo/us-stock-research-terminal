"""Read/write helpers for the application's own tables.

All SQL is fixed text with bound parameters. Nothing here accepts SQL from callers,
and nothing here is meant to be exposed to an LLM.
"""

from datetime import datetime

import duckdb
import polars as pl

from app.database.schema import META_TABLE, TABLES

QUERY_TEXT_MAX_LENGTH = 2000


def _frame(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> pl.DataFrame:
    return con.execute(sql, params or []).pl()


def schema_version(con: duckdb.DuckDBPyConnection) -> int | None:
    row = con.execute(f"SELECT value FROM {META_TABLE} WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else None


def table_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Row count per data table. Table names come from the fixed TABLES constant."""
    return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def last_sync(con: duckdb.DuckDBPyConnection) -> datetime | None:
    """Most recent data retrieval, or None if nothing was ever synchronized."""
    return con.execute("SELECT max(retrieved_at) FROM sources").fetchone()[0]


# --- query history ----------------------------------------------------------------


def record_query(con: duckdb.DuckDBPyConnection, text: str) -> int:
    text = text.strip()
    if not text:
        raise ValueError("Query text is empty.")
    if len(text) > QUERY_TEXT_MAX_LENGTH:
        raise ValueError(f"Query text is longer than {QUERY_TEXT_MAX_LENGTH} characters.")
    return con.execute("INSERT INTO query_history (query_text) VALUES (?) RETURNING query_id", [text]).fetchone()[0]


def recent_queries(con: duckdb.DuckDBPyConnection, limit: int = 10) -> pl.DataFrame:
    return _frame(
        con,
        "SELECT query_id, created_at, query_text, status FROM query_history ORDER BY query_id DESC LIMIT ?",
        [limit],
    )


# --- securities -------------------------------------------------------------------


def find_securities(con: duckdb.DuckDBPyConnection, ticker: str) -> pl.DataFrame:
    """Case-insensitive ticker lookup. May return several rows: tickers are not unique identities."""
    return _frame(
        con,
        "SELECT security_id, cik, ticker, name, exchange, sector, industry, is_active "
        "FROM securities WHERE upper(ticker) = upper(?) ORDER BY security_id",
        [ticker.strip()],
    )


def price_history(con: duckdb.DuckDBPyConnection, security_id: int) -> pl.DataFrame:
    return _frame(
        con,
        "SELECT trade_date, open, high, low, close, adj_close, volume FROM price_daily "
        "WHERE security_id = ? ORDER BY trade_date",
        [security_id],
    )


# --- watchlists -------------------------------------------------------------------


def list_watchlists(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    return _frame(
        con,
        "SELECT w.watchlist_id, w.name, w.description, w.created_at, count(i.security_id) AS items "
        "FROM watchlists w LEFT JOIN watchlist_items i USING (watchlist_id) "
        "GROUP BY ALL ORDER BY w.name",
    )


def create_watchlist(con: duckdb.DuckDBPyConnection, name: str, description: str | None = None) -> int:
    name = name.strip()
    if not name:
        raise ValueError("Watchlist name is empty.")
    exists = con.execute("SELECT 1 FROM watchlists WHERE lower(name) = lower(?)", [name]).fetchone()
    if exists:
        raise ValueError(f"A watchlist named '{name}' already exists.")
    return con.execute(
        "INSERT INTO watchlists (name, description) VALUES (?, ?) RETURNING watchlist_id",
        [name, description or None],
    ).fetchone()[0]
