"""Service layer: the only code the UI calls. Includes behaviour while a refresh owns the database."""

import pytest

from app.database import access
from app.database.locking import writer_status
from app.models.identifiers import InvalidCikError, InvalidTickerError
from app.models.records import PriceRecord, SecurityRecord
from app.services import company_service, query_service, status_service, watchlist_service
from app.services.errors import ConfigurationError, DataUnavailableError
from app.services.settings_service import get_ollama_status, get_settings
from app.services.startup_service import prepare_database


@pytest.fixture(autouse=True)
def fresh_cache():
    status_service.forget_last_reading()
    yield
    status_service.forget_last_reading()


@pytest.fixture
def settings(app_env, db_path):  # app_env and db_path share one temp path
    return get_settings()


# --- status: live / stale / unavailable --------------------------------------------------------------


def test_status_of_a_missing_database_does_not_create_it(app_env):
    status = status_service.collect_data_status(get_settings())
    assert status.data_state == "unavailable" and status.database.exists is False
    assert status.counts == {} and status.last_sync is None
    assert not app_env.exists()


def test_status_of_an_initialised_database_is_live(settings):
    status = status_service.collect_data_status(settings)
    assert status.data_state == "live" and status.notice is None
    assert status.database.initialized and status.database.schema_version == 2
    assert status.counts["securities"] == 0 and status.last_sync is None
    assert status.as_of is not None and status.refresh.active is False
    assert status.openbb.all_installed and status.ollama.available is False


def test_status_counts_reflect_stored_rows(settings, db_path):
    with access.writer(db_path, "test") as w:
        sid = w.upsert_security(SecurityRecord(ticker="TST", cik=1))
        import datetime as dt

        w.upsert_prices([PriceRecord(security_id=sid, trade_date=dt.date(2024, 1, 2), close=1.0)])
    status = status_service.collect_data_status(settings)
    assert (status.counts["securities"], status.counts["price_daily"]) == (1, 1)


def test_corrupt_database_is_reported_not_raised(app_env):
    app_env.write_bytes(b"this is not a duckdb file" * 100)
    status = status_service.collect_data_status(get_settings())
    assert status.data_state == "unavailable" and status.database.error


def test_during_a_refresh_status_is_cached_and_clearly_marked_stale(settings, hold):
    before = status_service.collect_data_status(settings)
    assert before.data_state == "live"
    with hold("writer") as holder:
        during = status_service.collect_data_status(settings)
        assert during.data_state == "stale"  # never presented as live
        assert during.as_of == before.as_of  # the timestamp of the data actually shown, not "now"
        assert during.counts == before.counts
        assert during.refresh.active and during.refresh.operation == "data refresh" and during.refresh.pid == holder.pid
        assert "refresh" in during.notice and "cached" in during.notice and "out of date" in during.notice
    after = status_service.collect_data_status(settings)
    assert after.data_state == "live" and after.refresh.active is False


def test_during_a_refresh_without_any_earlier_reading_status_is_unavailable_not_invented(settings, hold):
    with hold("writer"):
        status = status_service.collect_data_status(settings)
    assert status.data_state == "unavailable"
    assert status.counts == {} and status.as_of is None
    assert "No earlier reading" in status.notice and "refresh" in status.notice


def test_a_lock_held_by_something_that_is_not_a_refresh_is_not_called_a_refresh(settings, hold):
    with hold("duckdb-only"):  # some other tool has the file open; no writer lock is held
        status = status_service.collect_data_status(settings)
    assert status.data_state == "unavailable" and status.refresh.active is False
    assert "refresh" not in status.notice.lower()


# --- user actions while a refresh owns the database ----------------------------------------------------


def test_saving_a_request_during_a_refresh_fails_clearly_and_writes_nothing(settings, db_path, hold):
    with hold("writer"):
        with pytest.raises(DataUnavailableError, match="being updated") as raised:
            query_service.submit_query("tech stocks")
        assert raised.value.refresh.active and raised.value.refresh.operation == "data refresh"
    assert query_service.recent_queries().is_empty()  # nothing slipped in
    assert query_service.submit_query("tech stocks") == 1  # and it works once the refresh is over


def test_reading_during_a_refresh_fails_clearly(settings, hold):
    with hold("writer"):
        for call in (
            query_service.recent_queries,
            watchlist_service.list_watchlists,
            lambda: company_service.find_companies("AAPL"),
        ):
            with pytest.raises(DataUnavailableError, match="Live data is unavailable"):
                call()


def test_a_user_write_releases_the_writer_lock_afterwards(settings, db_path):
    query_service.submit_query("hello")
    assert writer_status(db_path).active is False


# --- query / company / watchlist services ----------------------------------------------------------------


def test_query_service_validates_text(settings):
    assert query_service.submit_query("  hello  ") == 1
    assert query_service.recent_queries()["query_text"].to_list() == ["hello"]
    for bad in ("", "  ", "x" * (query_service.QUERY_TEXT_MAX_LENGTH + 1)):
        with pytest.raises(ValueError):
            query_service.submit_query(bad)


def test_company_lookup_by_ticker_or_any_spelling_of_the_cik(settings, db_path):
    with access.writer(db_path, "test") as w:
        w.upsert_security(SecurityRecord(ticker="AAPL", cik=320193, name="Apple Inc."))
    for query in ("aapl", " AAPL ", "320193", "0000320193", "CIK320193", "  320193  "):
        assert company_service.find_companies(query)["ticker"].to_list() == ["AAPL"], query
    assert company_service.find_companies("ZZZZ").is_empty()
    assert company_service.find_companies("999999").is_empty()
    with pytest.raises(InvalidTickerError):
        company_service.find_companies("not a ticker!")
    with pytest.raises(InvalidCikError):
        company_service.find_companies("12345678901")  # digits only, but too long to be a CIK


def test_watchlist_service(settings):
    assert watchlist_service.list_watchlists().is_empty()
    watchlist_service.create_watchlist("Growth", "high growth")
    with pytest.raises(ValueError, match="already exists"):
        watchlist_service.create_watchlist("growth")
    assert watchlist_service.list_watchlists()["name"].to_list() == ["Growth"]


# --- start-up and settings -----------------------------------------------------------------------------


def test_prepare_database_creates_it(app_env):
    assert prepare_database().ready is True
    assert app_env.is_file()


def test_prepare_database_never_raises_when_the_database_is_busy(settings, hold):
    with hold("writer"):
        result = prepare_database()
    assert result.ready is False and "busy" in result.message


def test_prepare_database_reports_a_corrupt_file(app_env):
    app_env.write_bytes(b"garbage" * 1000)
    result = prepare_database()
    assert result.ready is False and "could not be initialised" in result.message


def test_settings_service_reports_invalid_configuration(clean_env):
    clean_env.setenv("FAST_THINK", "maybe")
    with pytest.raises(ConfigurationError):
        get_settings()


def test_ollama_status_through_the_service_never_raises(app_env):
    assert get_ollama_status(get_settings()).available is False
