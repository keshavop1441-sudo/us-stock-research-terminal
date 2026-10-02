import ast
import datetime as dt
import inspect
from pathlib import Path

import pytest

from app.database import access, read_repository, write_repository
from app.database.quality_repository import QualityRepository
from app.database.read_repository import ReadRepository
from app.database.schema import SCHEMA_VERSION
from app.database.write_repository import WriteRepository
from app.models.identifiers import InvalidCikError, InvalidTickerError
from app.models.records import PriceRecord, SecurityRecord, SourceRecord

ROOT = Path(__file__).resolve().parent.parent
RAW_ACCESS_NAMES = {
    "execute",
    "executemany",
    "sql",
    "query",
    "run",
    "cursor",
    "connection",
    "con",
    "conn",
    "raw",
    "fetchall",
}


def test_reads_return_polars_frames_with_stable_types(db_path):
    with access.reader(db_path) as r:
        empty = r.price_history(1)
        assert empty.is_empty() and empty.columns[:2] == ["trade_date", "open"]
        assert r.find_securities_by_ticker("AAPL").is_empty()
        assert r.recent_queries().is_empty()
        assert r.list_watchlists().is_empty()
        assert r.schema_version() == SCHEMA_VERSION
        assert r.last_sync() is None


def test_reads_see_what_the_writer_committed(db_path):
    with access.writer(db_path, "test") as w:
        sid = w.upsert_security(SecurityRecord(ticker="AAPL", cik=320193, name="Apple Inc."))
        w.upsert_prices(
            [
                PriceRecord(security_id=sid, trade_date=dt.date(2024, 1, 3), close=2.0),
                PriceRecord(security_id=sid, trade_date=dt.date(2024, 1, 2)),
            ]
        )
        w.record_source(SourceRecord(provider="test", dataset="fixture"))
        w.record_query("hello")
        w.create_watchlist("Growth")
    with access.reader(db_path) as r:
        history = r.price_history(sid)
        assert history["trade_date"].to_list() == [dt.date(2024, 1, 2), dt.date(2024, 1, 3)]
        assert history["close"].to_list() == [None, 2.0]  # missing stays NULL
        assert r.last_sync() is not None
        assert r.table_counts()["price_daily"] == 2
        assert r.recent_queries()["query_text"].to_list() == ["hello"]
        assert r.list_watchlists()["name"].to_list() == ["Growth"]


def test_lookups_use_the_canonical_forms(db_path):
    with access.writer(db_path, "test") as w:
        w.upsert_security(SecurityRecord(ticker="GOOG", cik=1652044))
        w.upsert_security(SecurityRecord(ticker="GOOGL", cik=1652044))
    with access.reader(db_path) as r:
        for spelling in (1652044, "1652044", "0001652044", "  1652044  ", "CIK1652044"):
            assert r.find_securities_by_cik(spelling)["ticker"].to_list() == ["GOOG", "GOOGL"]
        for spelling in ("goog", " GOOG ", "Goog"):
            assert r.find_securities_by_ticker(spelling)["ticker"].to_list() == ["GOOG"]
        with pytest.raises(InvalidCikError):
            r.find_securities_by_cik("not-a-cik")
        with pytest.raises(InvalidTickerError):
            r.find_securities_by_ticker("'; DROP TABLE securities; --")


def test_read_repository_exposes_only_named_read_methods():
    public = {n for n, _ in inspect.getmembers(ReadRepository, inspect.isfunction) if not n.startswith("_")}
    assert public == {
        "schema_version", "table_counts", "last_sync", "recent_queries", "find_securities_by_ticker",
        "find_securities_by_cik", "price_history", "list_watchlists",
        "market_quotes", "securities_of_issuers", "financial_facts", "filings",  # v3 (Phase 3A) named reads
    }  # fmt: skip
    assert not public & RAW_ACCESS_NAMES


def test_neither_repository_offers_raw_sql_or_a_public_connection(db_path):
    for cls in (ReadRepository, WriteRepository, QualityRepository):
        members = {n for n in dir(cls) if not n.startswith("_")}
        assert not members & RAW_ACCESS_NAMES, (cls.__name__, members & RAW_ACCESS_NAMES)
        for name, method in inspect.getmembers(cls, inspect.isfunction):
            if name.startswith("_"):
                continue
            for parameter in inspect.signature(method).parameters:
                assert parameter not in {"sql", "query_sql", "statement", "command", "script"}, (cls.__name__, name)
    with access.reader(db_path) as r:
        assert not [a for a in vars(r) if not a.startswith("_")]  # the connection is private


def test_read_repository_cannot_write_and_does_not_import_the_write_side():
    tree = ast.parse(Path(read_repository.__file__).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert "app.database.write_repository" not in imported
    assert "app.database.access" not in imported
    sql_strings = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER"))
    ]
    assert not sql_strings, sql_strings


def test_write_repository_does_not_import_the_read_side():
    assert "read_repository" not in Path(write_repository.__file__).read_text(encoding="utf-8")
