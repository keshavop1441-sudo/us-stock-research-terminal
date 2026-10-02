import datetime as dt

import duckdb
import pytest

from app.database import migrations
from app.database.connection import ensure_database
from app.database.errors import MigrationError, SchemaVersionError
from app.database.schema import (
    DDL_V2_TABLES,
    META_TABLE,
    SCHEMA_VERSION,
    TABLES,
    V1_OBSOLETE_SEQUENCES,
    V2_RECREATED_TABLES,
)

EXPECTED_TABLES = {
    "securities", "financial_facts", "price_daily", "filings", "earnings", "ownership",
    "events", "sources", "research_runs", "query_history", "watchlists", "watchlist_items", "market_quotes",
}  # fmt: skip


def table_names(con):
    return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}


def test_creates_database_file_and_schema(tmp_path):
    path = tmp_path / "nested" / "research.duckdb"
    assert ensure_database(path) == SCHEMA_VERSION
    assert path.is_file()
    con = duckdb.connect(str(path))
    assert table_names(con) == EXPECTED_TABLES | {META_TABLE}
    assert migrations.read_schema_version(con) == SCHEMA_VERSION
    con.close()


def test_table_set_is_exactly_the_specified_one():
    assert set(TABLES) == EXPECTED_TABLES


def test_no_per_ratio_tables(con):
    assert not [t for t in table_names(con) if any(w in t for w in ("ratio", "margin", "growth", "valuation"))]


def test_fresh_database_is_empty(con):
    assert {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES} == dict.fromkeys(TABLES, 0)


def test_ensure_is_idempotent_and_keeps_data(db_path, con):
    con.execute("INSERT INTO query_history (query_text) VALUES ('keep me')")
    con.close()
    assert ensure_database(db_path) == SCHEMA_VERSION
    again = duckdb.connect(str(db_path))
    assert again.execute("SELECT query_text FROM query_history").fetchall() == [("keep me",)]
    again.close()


def test_refuses_database_from_newer_version(db_path, con):
    con.execute(f"UPDATE {META_TABLE} SET value = ? WHERE key = 'schema_version'", [str(SCHEMA_VERSION + 1)])
    con.close()
    with pytest.raises(SchemaVersionError):
        ensure_database(db_path)


def test_cik_must_be_ten_digit_text_in_every_table(con):
    con.execute("INSERT INTO securities (cik, ticker) VALUES ('0000320193', 'AAPL')")
    con.execute("INSERT INTO securities (ticker) VALUES ('NOCIK')")  # CIK is optional here
    with pytest.raises(duckdb.ConstraintException):
        con.execute("INSERT INTO securities (cik, ticker) VALUES ('320193', 'BAD')")
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "INSERT INTO filings (accession_no, cik, form, filing_date) VALUES ('a', '12', '10-K', '2024-01-01')"
        )
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "INSERT INTO earnings (cik, fiscal_year, fiscal_period, report_date) VALUES ('1', 2024, 'Q1', '2024-01-01')"
        )


def test_business_key_columns_must_match_their_components(con):
    """The *_key primary keys are tied to their component columns by CHECK constraints."""
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "INSERT INTO financial_facts (fact_key, cik, taxonomy, concept, unit, value, period_end) "
            "VALUES ('wrong', '0000000001', 'us-gaap', 'Revenues', 'USD', 1, '2024-12-31')"
        )
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "INSERT INTO events (event_key, cik, event_type, source_ref, title) "
            "VALUES ('0000000002|news|http://x', '0000000001', 'news', 'http://x', 't')"
        )
    con.execute(
        "INSERT INTO events (event_key, cik, event_type, source_ref, title) "
        "VALUES ('0000000001|news|http://x', '0000000001', 'news', 'http://x', 't')"
    )


def test_ticker_is_not_identity(con):
    """A ticker can change or be reused; security_id and CIK stay stable."""
    con.execute("INSERT INTO securities (cik, ticker) VALUES ('0000000001', 'OLD')")
    con.execute("INSERT INTO price_daily (security_id, trade_date, close) VALUES (1, '2024-01-02', 1.5)")
    con.execute("UPDATE securities SET ticker = 'NEW' WHERE security_id = 1")  # child rows exist: must still work
    con.execute("INSERT INTO securities (cik, ticker) VALUES ('0000000002', 'OLD')")  # ticker reused by another issuer
    assert con.execute("SELECT cik FROM securities WHERE ticker = 'NEW'").fetchall() == [("0000000001",)]


def test_foreign_keys_are_enforced(con):
    with pytest.raises(duckdb.ConstraintException):
        con.execute("INSERT INTO price_daily (security_id, trade_date) VALUES (999, '2024-01-02')")


def test_timestamps_default_to_utc_whatever_the_session_time_zone(con):
    """DuckDB would otherwise store local time for ``current_timestamp`` (a real problem on Windows)."""
    con.execute("SET TimeZone = 'Pacific/Kiritimati'")  # UTC+14: any leak of local time is unmistakable
    con.execute("INSERT INTO query_history (query_text) VALUES ('x')")
    stored = con.execute("SELECT created_at FROM query_history").fetchone()[0]
    now_utc = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    assert abs((now_utc - stored).total_seconds()) < 60


def _make_v1_database(path):
    """Build a schema-v1 database: old key layout, zone-dependent defaults, one saved request."""
    from app.database import schema

    con = duckdb.connect(str(path))
    con.execute("BEGIN")
    for statement in schema.DDL:
        if "financial_facts" in statement.split("(")[0] or any(
            t in statement.split("(")[0] for t in ("earnings", "ownership", "events")
        ):
            continue  # created below in their v1 shape
        con.execute(statement.replace(schema.NOW_UTC, "current_timestamp"))
    for sequence in V1_OBSOLETE_SEQUENCES:
        con.execute(f"CREATE SEQUENCE {sequence}")
    con.execute("CREATE TABLE financial_facts (fact_id BIGINT PRIMARY KEY DEFAULT nextval('seq_fact_id'), cik VARCHAR)")
    con.execute(
        "CREATE TABLE earnings (earnings_id BIGINT PRIMARY KEY DEFAULT nextval('seq_earnings_id'), cik VARCHAR)"
    )
    con.execute(
        "CREATE TABLE ownership (ownership_id BIGINT PRIMARY KEY DEFAULT nextval('seq_ownership_id'), cik VARCHAR)"
    )
    con.execute("CREATE TABLE events (event_id BIGINT PRIMARY KEY DEFAULT nextval('seq_event_id'), cik VARCHAR)")
    con.execute("INSERT INTO schema_meta VALUES ('schema_version', '1')")
    con.execute("INSERT INTO query_history (query_text) VALUES ('saved with v1')")
    con.execute("INSERT INTO securities (ticker) VALUES ('KEEP')")
    con.execute("COMMIT")
    con.close()


def test_migration_from_v1_keeps_user_data_and_recreates_keyed_tables(tmp_path):
    path = tmp_path / "v1.duckdb"
    _make_v1_database(path)
    assert ensure_database(path) == SCHEMA_VERSION

    con = duckdb.connect(str(path))
    assert migrations.read_schema_version(con) == SCHEMA_VERSION
    assert con.execute("SELECT query_text FROM query_history").fetchall() == [("saved with v1",)]
    assert con.execute("SELECT ticker FROM securities").fetchall() == [("KEEP",)]
    for table in V2_RECREATED_TABLES:
        columns = {r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()}
        assert {
            "financial_facts": "fact_key",
            "earnings": "fiscal_period",
            "ownership": "ownership_key",
            "events": "event_key",
        }[table] in columns
    sequences = {r[0] for r in con.execute("SELECT sequence_name FROM duckdb_sequences()").fetchall()}
    assert not sequences & set(V1_OBSOLETE_SEQUENCES)
    # the new UTC default is active on a table that existed in v1 (and is referenced by foreign keys)
    con.execute("SET TimeZone = 'Pacific/Kiritimati'")
    con.execute("INSERT INTO securities (ticker) VALUES ('NEW')")
    stamp = con.execute("SELECT updated_at FROM securities WHERE ticker = 'NEW'").fetchone()[0]
    assert abs((dt.datetime.now(dt.UTC).replace(tzinfo=None) - stamp).total_seconds()) < 60
    con.close()
    assert ensure_database(path) == SCHEMA_VERSION  # and re-running is a no-op


def test_migration_refuses_to_drop_a_table_that_has_rows(tmp_path):
    path = tmp_path / "v1.duckdb"
    _make_v1_database(path)
    con = duckdb.connect(str(path))
    con.execute("INSERT INTO financial_facts (cik) VALUES ('0000000001')")
    con.close()
    with pytest.raises(MigrationError, match="financial_facts"):
        ensure_database(path)
    con = duckdb.connect(str(path))  # nothing was changed: the migration is one transaction
    assert migrations.read_schema_version(con) == 1
    assert con.execute("SELECT count(*) FROM financial_facts").fetchone()[0] == 1
    con.close()


def test_v2_ddl_covers_all_recreated_tables():
    assert set(DDL_V2_TABLES) == set(V2_RECREATED_TABLES)


# --- v2 -> v3 (Phase 3A) ------------------------------------------------------------------------------------------


def _make_v2_database(path):
    """A schema-v2 database with rows in tables that foreign keys reference (sources, securities)."""
    from app.database import schema

    con = duckdb.connect(str(path))
    con.execute("BEGIN")
    for statement in schema.DDL:
        con.execute(statement)
    con.execute("INSERT INTO schema_meta VALUES ('schema_version', '2')")
    con.execute("INSERT INTO sources (provider, dataset) VALUES ('sec', 'companyfacts')")
    con.execute("INSERT INTO securities (ticker, cik) VALUES ('AAPL', '0000320193')")
    con.execute("INSERT INTO price_daily (security_id, trade_date, close, source_id) VALUES (1, '2026-01-02', 1.5, 1)")
    con.execute(
        "INSERT INTO financial_facts VALUES ('0000320193|us-gaap|Revenues|USD||2025-09-27|0000320193-25-000079', "
        "'0000320193', 'us-gaap', 'Revenues', 'USD', 1.0, NULL, '2025-09-27', 2025, 'FY', '10-K', '2025-10-31', "
        "'0000320193-25-000079', 1)"
    )
    con.execute("COMMIT")
    con.close()


def _columns(con):
    return {
        t: [(r[0], r[1]) for r in con.execute(f"DESCRIBE {t}").fetchall()]
        for t in [*TABLES, "schema_meta"]
        if t != "schema_meta"
    }


def test_migration_v2_to_v3_keeps_rows_and_adds_columns(tmp_path):
    path = tmp_path / "v2.duckdb"
    _make_v2_database(path)
    assert ensure_database(path) == SCHEMA_VERSION == 3
    con = duckdb.connect(str(path))
    assert con.execute("SELECT ticker, cik, sector_source, sic_source FROM securities").fetchall() == [
        ("AAPL", "0000320193", None, None)
    ]
    row = con.execute(
        "SELECT provider, command, parameters, provider_version, as_of, is_fallback FROM sources"
    ).fetchall()
    assert row == [("sec", None, None, None, None, False)]
    assert con.execute("SELECT count(*), count(frame) FROM financial_facts").fetchone() == (1, 0)
    assert con.execute("SELECT count(*) FROM price_daily").fetchone()[0] == 1
    owner_columns = {r[0] for r in con.execute("DESCRIBE ownership").fetchall()}
    assert {"transaction_price", "shares_owned_after", "acquired_disposed", "is_derivative", "security_title",
            "ownership_nature", "is_10b5_1"} <= owner_columns  # fmt: skip
    assert con.execute("SELECT count(*) FROM market_quotes").fetchone()[0] == 0
    con.close()
    assert ensure_database(path) == 3  # a second run is a no-op


def test_upgraded_and_fresh_databases_have_identical_shape(tmp_path):
    _make_v2_database(tmp_path / "old.duckdb")
    ensure_database(tmp_path / "old.duckdb")
    ensure_database(tmp_path / "new.duckdb")
    old, new = duckdb.connect(str(tmp_path / "old.duckdb")), duckdb.connect(str(tmp_path / "new.duckdb"))
    assert _columns(old) == _columns(new)
    old.close()
    new.close()


def test_v1_database_walks_through_every_migration_to_v3(tmp_path):
    path = tmp_path / "v1.duckdb"
    _make_v1_database(path)
    assert ensure_database(path) == 3
    con = duckdb.connect(str(path))
    assert "frame" in {r[0] for r in con.execute("DESCRIBE financial_facts").fetchall()}
    con.close()


def test_failed_v3_migration_rolls_back_completely(tmp_path, monkeypatch):
    path = tmp_path / "v2.duckdb"
    _make_v2_database(path)
    monkeypatch.setitem(migrations.DDL_V3_TABLES, "market_quotes", "CREATE TABLE market_quotes (broken syntax")
    with pytest.raises(duckdb.Error):
        ensure_database(path)
    con = duckdb.connect(str(path))
    assert migrations.read_schema_version(con) == 2
    assert "frame" not in {r[0] for r in con.execute("DESCRIBE financial_facts").fetchall()}  # ALTERs were rolled back
    con.close()
