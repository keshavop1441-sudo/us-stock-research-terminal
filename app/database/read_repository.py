"""Explicit, parameterised READ operations.

This is the only database interface the research commands read through. It exposes named read methods,
never SQL, and holds no write operations. It does NOT depend on ``write_repository``.
(A DuckDB ``read_only`` connection cannot coexist with the application's read-write connections in one
process, so the boundary is the interface, not a connection flag.)
"""

import json

import duckdb
import polars as pl

from app.database.frames import to_polars
from app.database.migrations import read_schema_version
from app.database.schema import TABLES
from app.models.identifiers import normalize_cik, normalize_ticker

# Named tuple, not `except ValueError, AttributeError:` (3.14-only syntax; see app/database/locking.py).
_BAD_DETAIL = (ValueError, AttributeError)
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

    def last_price_bar(self, security_id: int) -> tuple | None:
        """(trade_date, close) of the newest stored price bar of a listing, or None if it has none."""
        return self._con.execute(
            "SELECT trade_date, close FROM price_daily WHERE security_id = ? ORDER BY trade_date DESC LIMIT 1",
            [security_id],
        ).fetchone()

    def market_quotes(self, security_id: int) -> pl.DataFrame:
        """Provider quotes of one listing, newest first."""
        return self._frame(
            "SELECT quote_date, last_price, market_cap, year_high, year_low FROM market_quotes "
            "WHERE security_id = ? ORDER BY quote_date DESC",
            [security_id],
        )

    def securities_of_issuers(self) -> pl.DataFrame:
        """Every stored listing with its issuer and classification provenance."""
        return self._frame(
            "SELECT security_id, cik, ticker, name, exchange, sector, industry, sector_source, sic, sic_source "
            "FROM securities ORDER BY cik, ticker"
        )

    # --- provenance of what a research answer was built from ---------------------------------------------------------

    _SOURCE_COLUMNS = (
        "s.source_id, s.provider, s.dataset, s.command, s.url, s.retrieved_at, s.as_of, s.content_hash, "
        "s.is_fallback, s.provider_version"
    )

    def price_source(self, security_id: int) -> dict | None:
        """The retrieval that produced the NEWEST stored price bar of a listing."""
        rows = self._frame(
            f"SELECT {self._SOURCE_COLUMNS} FROM price_daily p JOIN sources s ON s.source_id = p.source_id "
            "WHERE p.security_id = ? ORDER BY p.trade_date DESC LIMIT 1",
            [security_id],
        ).to_dicts()
        return rows[0] if rows else None

    def quote_source(self, security_id: int) -> dict | None:
        """The retrieval that produced the NEWEST stored provider quote of a listing."""
        rows = self._frame(
            f"SELECT {self._SOURCE_COLUMNS} FROM market_quotes q JOIN sources s ON s.source_id = q.source_id "
            "WHERE q.security_id = ? ORDER BY q.quote_date DESC LIMIT 1",
            [security_id],
        ).to_dicts()
        return rows[0] if rows else None

    def facts_source(self, cik: int | str) -> dict | None:
        """The latest SEC ``companyfacts`` retrieval of an issuer (it also explains an unsupported taxonomy)."""
        parameters = json.dumps({"cik": normalize_cik(cik)}, sort_keys=True)
        rows = self._frame(
            f"SELECT {self._SOURCE_COLUMNS} FROM sources s WHERE s.provider = 'sec' AND s.dataset = 'companyfacts' "
            "AND s.parameters = ? ORDER BY s.retrieved_at DESC, s.source_id DESC LIMIT 1",
            [parameters],
        ).to_dicts()
        return rows[0] if rows else None

    # --- accounting facts and filings ---------------------------------------------------------------

    def financial_facts(self, cik: int | str) -> pl.DataFrame:
        """Every stored as-reported fact of an issuer (all vintages)."""
        return self._frame(
            "SELECT taxonomy, concept, unit, value, period_start, period_end, fiscal_year, fiscal_period, form, "
            "filed_date, accession_no, frame FROM financial_facts WHERE cik = ? "
            "ORDER BY concept, period_end, filed_date, accession_no",
            [normalize_cik(cik)],
        )

    def fact_support_notes(self, cik: int | str) -> list[str]:
        """Notes the latest SEC companyfacts retrieval of an issuer recorded (e.g. ``UNSUPPORTED_TAXONOMY:ifrs-full``).

        They say WHY an issuer has no stored facts, so "no facts" is never read as "zero"."""
        parameters = json.dumps({"cik": normalize_cik(cik)}, sort_keys=True)
        row = self._con.execute(
            "SELECT detail FROM sources WHERE provider = 'sec' AND dataset = 'companyfacts' AND parameters = ? "
            "ORDER BY retrieved_at DESC, source_id DESC LIMIT 1",
            [parameters],
        ).fetchone()
        if not row or not row[0]:
            return []
        try:
            return [str(n) for n in json.loads(row[0]).get("notes", [])]
        except _BAD_DETAIL:
            return []

    def filings(self, cik: int | str) -> pl.DataFrame:
        return self._frame(
            "SELECT accession_no, form, filing_date, report_date, primary_document, url FROM filings "
            "WHERE cik = ? ORDER BY filing_date DESC, accession_no",
            [normalize_cik(cik)],
        )

    # --- watchlists --------------------------------------------------------------------

    def list_watchlists(self) -> pl.DataFrame:
        return self._frame(
            "SELECT w.watchlist_id, w.name, w.description, w.created_at, count(i.security_id) AS items "
            "FROM watchlists w LEFT JOIN watchlist_items i USING (watchlist_id) "
            "GROUP BY ALL ORDER BY w.name"
        )
