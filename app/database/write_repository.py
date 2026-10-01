"""Explicit WRITE operations: specific inserts and idempotent upserts, never arbitrary SQL.

Only reachable through ``app.database.access.writer`` which holds the single-writer lock and wraps the
work in one transaction. Methods accept validated record models (``app.models.records``), so canonical
CIKs/tickers and business keys are produced in one place. SQL is fixed text with bound parameters; the
only interpolated text is table/column names taken from this module's own constants.

Upsert semantics: re-loading the same logical record (same business key, see ``app.database.schema``)
updates it in place instead of adding a second row. Within one batch the LAST occurrence of a key wins
(DuckDB would silently keep the first). Optional attributes that are ``None`` on a *securities* upsert
never erase known values.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import duckdb

from app.database.schema import NOW_UTC
from app.models.records import (
    EarningsRecord,
    EventRecord,
    FilingRecord,
    FinancialFactRecord,
    OwnershipRecord,
    PriceRecord,
    SecurityRecord,
    SourceRecord,
)

QUERY_TEXT_MAX_LENGTH = 2000
_CHUNK_ROWS = 500


class AmbiguousSecurityError(ValueError):
    """A cik-less security record matches several existing securities with the same ticker."""


@dataclass(frozen=True)
class UpsertResult:
    inserted: int  # new logical records
    updated: int  # logical records that already existed and were refreshed


class WriteRepository:
    def __init__(self, connection: duckdb.DuckDBPyConnection):
        self._con = connection

    # --- generic idempotent upsert (private) -----------------------------------------------

    def _upsert(
        self,
        table: str,
        columns: Sequence[str],
        key_columns: Sequence[str],
        rows: Sequence[tuple],
    ) -> UpsertResult:
        key_positions = [columns.index(c) for c in key_columns]
        unique = {tuple(row[i] for i in key_positions): row for row in rows}  # last occurrence wins
        if not unique:
            return UpsertResult(0, 0)
        update_columns = [c for c in columns if c not in key_columns]
        conflict = f"ON CONFLICT ({', '.join(key_columns)}) DO " + (
            "UPDATE SET " + ", ".join(f"{c} = excluded.{c}" for c in update_columns) if update_columns else "NOTHING"
        )
        before = self._con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        batch = list(unique.values())
        for start in range(0, len(batch), _CHUNK_ROWS):
            chunk = batch[start : start + _CHUNK_ROWS]
            placeholders = ", ".join(["(" + ", ".join("?" * len(columns)) + ")"] * len(chunk))
            params = [value for row in chunk for value in row]
            self._con.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES {placeholders} {conflict}", params)
        after = self._con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        return UpsertResult(inserted=after - before, updated=len(unique) - (after - before))

    # --- application-owned tables ------------------------------------------------------------

    def record_query(self, text: str) -> int:
        text = text.strip()
        if not text:
            raise ValueError("Query text is empty.")
        if len(text) > QUERY_TEXT_MAX_LENGTH:
            raise ValueError(f"Query text is longer than {QUERY_TEXT_MAX_LENGTH} characters.")
        return self._con.execute(
            "INSERT INTO query_history (query_text) VALUES (?) RETURNING query_id", [text]
        ).fetchone()[0]

    def create_watchlist(self, name: str, description: str | None = None) -> int:
        name = name.strip()
        if not name:
            raise ValueError("Watchlist name is empty.")
        if self._con.execute("SELECT 1 FROM watchlists WHERE lower(name) = lower(?)", [name]).fetchone():
            raise ValueError(f"A watchlist named '{name}' already exists.")
        return self._con.execute(
            "INSERT INTO watchlists (name, description) VALUES (?, ?) RETURNING watchlist_id",
            [name, description or None],
        ).fetchone()[0]

    # --- provenance (append-only: one row per retrieval) -------------------------------------

    def record_source(self, source: SourceRecord) -> int:
        return self._con.execute(
            "INSERT INTO sources (provider, dataset, url, content_hash, detail) "
            "VALUES (?, ?, ?, ?, ?) RETURNING source_id",
            [source.provider, source.dataset, source.url, source.content_hash, source.detail],
        ).fetchone()[0]

    # --- securities: identity (ticker, cik) enforced here, see schema docstring --------------

    def upsert_security(self, record: SecurityRecord) -> int:
        """Insert or refresh a security and return its stable ``security_id``."""
        existing = self._con.execute(
            "SELECT security_id, cik FROM securities WHERE ticker = ? ORDER BY security_id", [record.ticker]
        ).fetchall()
        match = next((sid for sid, cik in existing if cik == record.cik), None)
        if match is None and record.cik is not None:
            match = next((sid for sid, cik in existing if cik is None), None)  # adopt a previously cik-less row
        if match is None and record.cik is None and existing:
            if len(existing) > 1:
                raise AmbiguousSecurityError(
                    f"Ticker {record.ticker} matches {len(existing)} securities; a CIK is required to choose."
                )
            match = existing[0][0]  # a cik-less source refers to the only security with this ticker
        attributes = [
            record.name, record.exchange, record.security_type, record.sector,
            record.industry, record.sic, record.is_active,
        ]  # fmt: skip
        if match is None:
            return self._con.execute(
                "INSERT INTO securities (cik, ticker, name, exchange, security_type, sector, industry, sic, is_active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING security_id",
                [record.cik, record.ticker, *attributes],
            ).fetchone()[0]
        self._con.execute(
            "UPDATE securities SET cik = coalesce(?, cik), name = coalesce(?, name), exchange = coalesce(?, exchange), "
            "security_type = coalesce(?, security_type), sector = coalesce(?, sector), "
            "industry = coalesce(?, industry), sic = coalesce(?, sic), is_active = coalesce(?, is_active), "
            f"updated_at = {NOW_UTC} WHERE security_id = ?",
            [record.cik, *attributes, match],
        )
        return match

    # --- issuer / security data: idempotent batch upserts ------------------------------------

    def upsert_prices(self, records: Sequence[PriceRecord]) -> UpsertResult:
        columns = ["security_id", "trade_date", "open", "high", "low", "close", "adj_close", "volume", "source_id"]
        return self._upsert("price_daily", columns, ["security_id", "trade_date"], _rows(records, columns))

    def upsert_financial_facts(self, records: Sequence[FinancialFactRecord]) -> UpsertResult:
        columns = [
            "fact_key", "cik", "taxonomy", "concept", "unit", "value", "period_start", "period_end",
            "fiscal_year", "fiscal_period", "form", "filed_date", "accession_no", "source_id",
        ]  # fmt: skip
        return self._upsert(
            "financial_facts", columns, ["fact_key"], _rows(records, columns, {"fact_key": lambda r: r.key})
        )

    def upsert_filings(self, records: Sequence[FilingRecord]) -> UpsertResult:
        columns = [
            "accession_no", "cik", "form", "filing_date", "report_date",
            "primary_document", "description", "url", "source_id",
        ]  # fmt: skip
        return self._upsert("filings", columns, ["accession_no"], _rows(records, columns))

    def upsert_earnings(self, records: Sequence[EarningsRecord]) -> UpsertResult:
        columns = [
            "cik", "fiscal_year", "fiscal_period", "report_date",
            "eps_actual", "eps_estimate", "revenue_actual", "revenue_estimate", "source_id",
        ]  # fmt: skip
        return self._upsert("earnings", columns, ["cik", "fiscal_year", "fiscal_period"], _rows(records, columns))

    def upsert_ownership(self, records: Sequence[OwnershipRecord]) -> UpsertResult:
        columns = [
            "ownership_key", "cik", "holder_type", "holder_key", "holder_name", "holder_cik", "as_of_date",
            "accession_no", "line_no", "shares", "value_usd", "shares_change", "transaction_code", "form", "source_id",
        ]  # fmt: skip
        return self._upsert(
            "ownership", columns, ["ownership_key"], _rows(records, columns, {"ownership_key": lambda r: r.key})
        )

    def upsert_events(self, records: Sequence[EventRecord]) -> UpsertResult:
        columns = [
            "event_key", "cik", "event_type", "source_ref", "event_date",
            "title", "summary", "url", "accession_no", "source_id",
        ]  # fmt: skip
        return self._upsert("events", columns, ["event_key"], _rows(records, columns, {"event_key": lambda r: r.key}))


def _rows(records: Sequence, columns: Sequence[str], overrides: dict[str, Callable] | None = None) -> list[tuple]:
    """Row tuples in ``columns`` order: record attributes, except where ``overrides`` supplies the value."""
    overrides = overrides or {}
    return [tuple(overrides[c](r) if c in overrides else getattr(r, c) for c in columns) for r in records]
