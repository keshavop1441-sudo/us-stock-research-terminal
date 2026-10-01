from app.config import Settings
from app.database.connection import connect
from app.services.status_service import collect_data_status


def settings_for(path):
    return Settings(database_path=path, ollama_base_url="http://127.0.0.1:9")


def test_missing_database(tmp_path):
    status = collect_data_status(settings_for(tmp_path / "missing.duckdb"))
    assert status.database.exists is False
    assert status.database.initialized is False
    assert status.counts == {}
    assert status.last_sync is None
    assert not (tmp_path / "missing.duckdb").exists()  # reporting status must not create it


def test_initialized_database(db_path):
    status = collect_data_status(settings_for(db_path))
    assert status.database.initialized is True
    assert status.database.schema_version == 1
    assert status.counts["securities"] == 0
    assert status.last_sync is None
    assert status.openbb.all_installed
    assert status.ollama.available is False


def test_counts_reflect_stored_rows(db_path):
    with connect(db_path) as con:
        con.execute("INSERT INTO sources (provider, dataset) VALUES ('test', 'fixture')")
        con.execute("INSERT INTO securities (ticker) VALUES ('TST')")
        con.execute("INSERT INTO price_daily (security_id, trade_date, close) VALUES (1, '2024-01-02', 1.0)")
    status = collect_data_status(settings_for(db_path))
    assert (status.counts["securities"], status.counts["price_daily"]) == (1, 1)
    assert status.last_sync is not None


def test_corrupt_database_is_reported_not_raised(tmp_path):
    path = tmp_path / "broken.duckdb"
    path.write_bytes(b"this is not a duckdb file" * 100)
    status = collect_data_status(settings_for(path))
    assert status.database.exists is True
    assert status.database.initialized is False
    assert status.database.error
