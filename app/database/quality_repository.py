"""Data-quality diagnostics: named, fixed SQL that counts rule violations and summarises retrievals.

Deliberately NOT part of ``ReadRepository``: that class is the only database interface the AI tool layer may ever
receive and its public surface is pinned by tests. These checks are for the ingestion/validation services
(``access.quality``).
"""

import duckdb
import polars as pl

from app.database.frames import to_polars


class QualityRepository:
    def __init__(self, connection: duckdb.DuckDBPyConnection):
        self._con = connection

    def _frame(self, sql: str) -> pl.DataFrame:
        return to_polars(self._con.execute(sql))

    def integrity_report(self) -> dict[str, int]:
        """Counts of rule violations. Every value must be 0 for a clean database (YAML acceptance A5, A6, A13)."""

        def count(sql: str) -> int:
            return self._con.execute(sql).fetchone()[0]

        def duplicates(table: str, key: str) -> int:
            return count(f"SELECT count(*) FROM (SELECT 1 FROM {table} GROUP BY {key} HAVING count(*) > 1)")

        cik_pattern = "'[0-9]{10}'"
        return {
            "duplicate_securities": duplicates("securities", "ticker, coalesce(cik, '')"),
            "duplicate_price_keys": duplicates("price_daily", "security_id, trade_date"),
            "duplicate_fact_keys": duplicates(
                "financial_facts",
                "cik, taxonomy, concept, unit, coalesce(period_start, DATE '1900-01-01'), period_end, "
                "coalesce(accession_no, '')",
            ),
            "duplicate_filings": duplicates("filings", "accession_no"),
            "duplicate_quote_keys": duplicates("market_quotes", "security_id, quote_date"),
            "prices_without_source": count("SELECT count(*) FROM price_daily WHERE source_id IS NULL"),
            "facts_without_source": count("SELECT count(*) FROM financial_facts WHERE source_id IS NULL"),
            "filings_without_source": count("SELECT count(*) FROM filings WHERE source_id IS NULL"),
            "quotes_without_source": count("SELECT count(*) FROM market_quotes WHERE source_id IS NULL"),
            "facts_without_accession_filed_or_form": count(
                "SELECT count(*) FROM financial_facts WHERE accession_no IS NULL OR filed_date IS NULL OR form IS NULL"
            ),
            "non_finite_prices": count(
                "SELECT count(*) FROM price_daily WHERE NOT (isfinite(coalesce(open, 0)) "
                "AND isfinite(coalesce(high, 0)) AND isfinite(coalesce(low, 0)) AND isfinite(coalesce(close, 0)))"
            ),
            "non_finite_facts": count("SELECT count(*) FROM financial_facts WHERE NOT isfinite(value)"),
            "bad_price_rows": count(
                "SELECT count(*) FROM price_daily WHERE close <= 0 OR high < low OR close > high * 1.0001 "
                "OR close < low * 0.9999"
            ),
            "adjusted_close_populated": count("SELECT count(*) FROM price_daily WHERE adj_close IS NOT NULL"),
            "unnormalised_ciks": count(
                "SELECT (SELECT count(*) FROM securities "
                f"WHERE cik IS NOT NULL AND NOT regexp_full_match(cik, {cik_pattern})) "
                f"+ (SELECT count(*) FROM financial_facts WHERE NOT regexp_full_match(cik, {cik_pattern})) "
                f"+ (SELECT count(*) FROM filings WHERE NOT regexp_full_match(cik, {cik_pattern}))"
            ),
            "classification_without_source": count(
                "SELECT count(*) FROM securities WHERE ((sector IS NOT NULL OR industry IS NOT NULL) "
                "AND sector_source IS NULL) OR (sic IS NOT NULL AND sic_source IS NULL)"
            ),
            "sources_missing_command_or_parameters": count(
                "SELECT count(*) FROM sources WHERE command IS NULL OR parameters IS NULL"
            ),
            "sources_missing_content_hash": count("SELECT count(*) FROM sources WHERE content_hash IS NULL"),
            "securities_without_cik": count("SELECT count(*) FROM securities WHERE cik IS NULL"),
        }

    def source_summary(self) -> pl.DataFrame:
        """Retrievals per provider/dataset with fallback and version information."""
        return self._frame(
            "SELECT provider, dataset, count(*) AS retrievals, "
            "sum(CASE WHEN is_fallback THEN 1 ELSE 0 END) AS fallbacks, count(provider_version) AS with_version, "
            "count(as_of) AS with_as_of, max(retrieved_at) AS last_retrieved "
            "FROM sources GROUP BY provider, dataset ORDER BY provider, dataset"
        )
