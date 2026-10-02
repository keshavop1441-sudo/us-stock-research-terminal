"""Service-layer database access (``app.services.db``): failures become DataUnavailableError, never stale data."""

import pytest

from app.database import access
from app.database.locking import writer_status
from app.models.records import PriceRecord, SecurityRecord
from app.services import db as service_db
from app.services.errors import ConfigurationError, DataUnavailableError
from app.services.settings_service import get_settings


@pytest.fixture
def settings(app_env, db_path):  # app_env and db_path share one temp path
    return get_settings()


def test_read_access_reads_what_a_writer_stored(settings, db_path):
    import datetime as dt

    with access.writer(db_path, "test") as w:
        sid = w.upsert_security(SecurityRecord(ticker="TST", cik=1))
        w.upsert_prices([PriceRecord(security_id=sid, trade_date=dt.date(2024, 1, 2), close=1.0)])
    with service_db.read_access() as r:
        assert r.find_securities_by_ticker("tst")["cik"].to_list() == ["0000000001"]
        assert r.price_history(sid).height == 1


def test_reading_during_a_refresh_fails_clearly(settings, hold):
    with hold("writer") as holder:
        with pytest.raises(DataUnavailableError, match="Live data is unavailable") as raised, service_db.read_access():
            pass
        assert raised.value.refresh.active and raised.value.refresh.pid == holder.pid


def test_writing_during_a_refresh_fails_clearly_and_writes_nothing(settings, db_path, hold):
    with hold("writer"):
        with pytest.raises(DataUnavailableError, match="being updated") as raised, service_db.write_access("t") as w:
            w.upsert_security(SecurityRecord(ticker="NOPE", cik=2))
        assert raised.value.refresh.active and raised.value.refresh.operation == "data refresh"
    with service_db.read_access() as r:
        assert r.find_securities_by_ticker("NOPE").is_empty()


def test_a_write_releases_the_writer_lock_afterwards(settings, db_path):
    with service_db.write_access("test write") as w:
        w.upsert_security(SecurityRecord(ticker="TST", cik=1))
    assert writer_status(db_path).active is False


def test_a_lock_held_by_something_that_is_not_a_refresh_is_not_called_a_refresh(settings, hold):
    with hold("duckdb-only"), pytest.raises(DataUnavailableError) as raised, service_db.read_access():
        pass  # some other tool has the file open; no writer lock is held
    assert raised.value.refresh.active is False
    assert "being updated" not in str(raised.value)


def test_settings_service_reports_invalid_configuration(clean_env):
    clean_env.setenv("SEC_USER_AGENT", "no-contact")
    with pytest.raises(ConfigurationError):
        get_settings()
