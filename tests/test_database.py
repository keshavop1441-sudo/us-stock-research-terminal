import duckdb
import pytest

from app.database import repository
from app.database.connection import SchemaVersionError, connect, init_database
from app.database.schema import META_TABLE, SCHEMA_VERSION, TABLES

EXPECTED_TABLES = {
    "securities", "financial_facts", "price_daily", "filings", "earnings", "ownership",
    "events", "sources", "research_runs", "query_history", "watchlists", "watchlist_items",
}  # fmt: skip


def _table_names(con):
    return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}


def test_creates_database_file_and_schema(tmp_path):
    path = tmp_path / "nested" / "research.duckdb"
    assert init_database(path) == SCHEMA_VERSION
    assert path.is_file()
    with connect(path) as con:
        assert _table_names(con) == EXPECTED_TABLES | {META_TABLE}
        assert repository.schema_version(con) == SCHEMA_VERSION


def test_table_set_is_exactly_the_specified_one():
    assert set(TABLES) == EXPECTED_TABLES


def test_no_per_ratio_tables(con):
    assert not [t for t in _table_names(con) if any(w in t for w in ("ratio", "margin", "growth", "valuation"))]


def test_fresh_database_is_empty(con):
    assert set(repository.table_counts(con).values()) == {0}
    assert repository.last_sync(con) is None


def test_init_is_idempotent_and_keeps_data(db_path):
    with connect(db_path) as con:
        repository.record_query(con, "keep me")
    assert init_database(db_path) == SCHEMA_VERSION
    with connect(db_path) as con:
        assert repository.table_counts(con)["query_history"] == 1


def test_refuses_database_from_newer_version(db_path):
    with connect(db_path) as con:
        con.execute(f"UPDATE {META_TABLE} SET value = ? WHERE key = 'schema_version'", [str(SCHEMA_VERSION + 1)])
    with pytest.raises(SchemaVersionError):
        init_database(db_path)


def test_cik_must_be_ten_digit_text(con):
    con.execute("INSERT INTO securities (cik, ticker) VALUES ('0000320193', 'AAPL')")
    con.execute("INSERT INTO securities (ticker) VALUES ('NOCIK')")  # CIK is optional
    with pytest.raises(duckdb.ConstraintException):
        con.execute("INSERT INTO securities (cik, ticker) VALUES ('320193', 'BAD')")
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "INSERT INTO filings (accession_no, cik, form, filing_date) VALUES ('a', '12', '10-K', '2024-01-01')"
        )


def test_ticker_is_not_identity(con):
    """A ticker can change or be reused; security_id and CIK stay stable."""
    con.execute("INSERT INTO securities (cik, ticker) VALUES ('0000000001', 'OLD')")
    security_id = con.execute("SELECT security_id FROM securities").fetchone()[0]
    con.execute("INSERT INTO price_daily (security_id, trade_date, close) VALUES (?, '2024-01-02', 1.5)", [security_id])
    con.execute("UPDATE securities SET ticker = 'NEW' WHERE security_id = ?", [security_id])  # child rows exist
    con.execute("INSERT INTO securities (cik, ticker) VALUES ('0000000002', 'OLD')")  # ticker reused
    assert repository.find_securities(con, "old").height == 1
    assert repository.find_securities(con, "new")["cik"].to_list() == ["0000000001"]


def test_two_share_classes_share_one_cik(con):
    con.execute("INSERT INTO securities (cik, ticker) VALUES ('0001652044', 'GOOG'), ('0001652044', 'GOOGL')")
    assert repository.table_counts(con)["securities"] == 2


def test_foreign_keys_are_enforced(con):
    with pytest.raises(duckdb.ConstraintException):
        con.execute("INSERT INTO price_daily (security_id, trade_date) VALUES (999, '2024-01-02')")


def test_price_history_and_last_sync(con):
    con.execute("INSERT INTO sources (provider, dataset) VALUES ('test', 'fixture')")
    con.execute("INSERT INTO securities (ticker) VALUES ('TST')")
    con.execute(
        "INSERT INTO price_daily (security_id, trade_date, close) "
        "VALUES (1, '2024-01-03', 2.0), (1, '2024-01-02', NULL)"
    )
    history = repository.price_history(con, 1)
    assert history["trade_date"].dt.day().to_list() == [2, 3]  # ordered
    assert history["close"].to_list() == [None, 2.0]  # missing stays NULL
    assert repository.last_sync(con) is not None
    assert repository.table_counts(con)["price_daily"] == 2


def test_query_history(con):
    first = repository.record_query(con, "  tech stocks  ")
    second = repository.record_query(con, "software")
    recent = repository.recent_queries(con)
    assert recent["query_id"].to_list() == [second, first]
    assert recent["query_text"].to_list() == ["software", "tech stocks"]
    assert set(recent["status"]) == {"received"}
    with pytest.raises(ValueError):
        repository.record_query(con, "   ")
    with pytest.raises(ValueError):
        repository.record_query(con, "x" * (repository.QUERY_TEXT_MAX_LENGTH + 1))


def test_queries_are_stored_literally_not_executed(con):
    repository.record_query(con, "'; DROP TABLE securities; --")
    assert "securities" in _table_names(con)
    assert repository.recent_queries(con)["query_text"].to_list() == ["'; DROP TABLE securities; --"]


def test_watchlists(con):
    watchlist_id = repository.create_watchlist(con, " Growth ", "high growth")
    with pytest.raises(ValueError):
        repository.create_watchlist(con, "growth")  # case-insensitive duplicate
    with pytest.raises(ValueError):
        repository.create_watchlist(con, " ")
    con.execute("INSERT INTO securities (ticker) VALUES ('TST')")
    con.execute("INSERT INTO watchlist_items (watchlist_id, security_id) VALUES (?, 1)", [watchlist_id])
    lists = repository.list_watchlists(con)
    assert lists["name"].to_list() == ["Growth"]
    assert lists["items"].to_list() == [1]
    con.execute("UPDATE watchlists SET name = 'Renamed' WHERE watchlist_id = ?", [watchlist_id])  # has items
    assert repository.list_watchlists(con)["name"].to_list() == ["Renamed"]
