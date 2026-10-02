"""Explicit, parameterised READ operations.

This is the only database interface that may ever be handed to the AI tool layer. It exposes named
read methods, never SQL, and holds no write operations. It does NOT depend on ``write_repository``.
(A DuckDB ``read_only`` connection cannot coexist with the application's read-write connections in one
process, so the boundary is the interface, not a connection flag.)
"""

import duckdb
import polars as pl

from app.database.frames import to_polars
from app.database.migrations import read_schema_version
from app.database.schema import TABLES
from app.models.identifiers import normalize_cik, normalize_ticker

_SECURITY_COLUMNS = "security_id, cik, ticker, name, exchange, sector, industry, is_active"


class ReadRepository:
    def __init__(self, connection: duckdb.DuckDBPyConnection):
        self._con = connection

    def _frame(self, sql: str, params: list | None = None) -> pl.DataFrame:
        return to_polars(self._con.execute(sql, params or []))

    # --- database health ---------------------------------------------------------------

    def schema_version(self) -> int | None:
        return read_schema_version(self._con)

    def table_counts(self) -> dict[str, int]:
        """Row count per data table. Table names come from the fixed TABLES constant."""
        return {t: self._con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}

    def last_sync(self):
        """Most recent data retrieval (datetime), or None if nothing was ever synchronized."""
        return self._con.execute("SELECT max(retrieved_at) FROM sources").fetchone()[0]

    # --- query history -----------------------------------------------------------------

    def recent_queries(self, limit: int = 10) -> pl.DataFrame:
        return self._frame(
            "SELECT query_id, created_at, query_text, status FROM query_history ORDER BY query_id DESC LIMIT ?",
            [limit],
        )

    # --- securities --------------------------------------------------------------------

    def find_securities_by_ticker(self, ticker: str) -> pl.DataFrame:
        """May return several rows: a ticker is not a unique identity."""
        return self._frame(
            f"SELECT {_SECURITY_COLUMNS} FROM securities WHERE ticker = ? ORDER BY security_id",
            [normalize_ticker(ticker)],
        )

    def find_securities_by_cik(self, cik: int | str) -> pl.DataFrame:
        """All listed securities of an issuer (e.g. GOOG and GOOGL share one CIK)."""
        return self._frame(
            f"SELECT {_SECURITY_COLUMNS} FROM securities WHERE cik = ? ORDER BY ticker, security_id",
            [normalize_cik(cik)],
        )

    def price_history(self, security_id: int) -> pl.DataFrame:
        return self._frame(
            "SELECT trade_date, open, high, low, close, adj_close, volume FROM price_daily "
            "WHERE security_id = ? ORDER BY trade_date",
            [security_id],
        )

    # --- watchlists --------------------------------------------------------------------

    def list_watchlists(self) -> pl.DataFrame:
        return self._frame(
            "SELECT w.watchlist_id, w.name, w.description, w.created_at, count(i.security_id) AS items "
            "FROM watchlists w LEFT JOIN watchlist_items i USING (watchlist_id) "
            "GROUP BY ALL ORDER BY w.name"
        )
