"""Phase 3A write path: inserted / updated / unchanged / duplicate accounting and the v3 provenance columns."""

import datetime as dt

import pytest
from pydantic import ValidationError

from app.database import access
from app.database.write_repository import UpsertResult
from app.models.records import (
    FinancialFactRecord,
    MarketQuoteRecord,
    OwnershipRecord,
    PriceRecord,
    SecurityRecord,
    SourceRecord,
)

D = dt.date


def price(sid, day, close, source_id=None):
    return PriceRecord(
        security_id=sid, trade_date=day, open=close, high=close, low=close, close=close, source_id=source_id
    )


def test_composite_key_batches_are_classified_per_record(db_path, con):
    with access.writer(db_path, "t") as w:
        sid = w.upsert_security(SecurityRecord(ticker="AAA"))
        first = [price(sid, D(2026, 1, 2), 1.0), price(sid, D(2026, 1, 3), 2.0)]
        assert w.upsert_prices(first) == UpsertResult(inserted=2, updated=0)
        batch = [price(sid, D(2026, 1, 2), 1.0), price(sid, D(2026, 1, 3), 2.5), price(sid, D(2026, 1, 4), 3.0)]
        assert w.upsert_prices(batch) == UpsertResult(inserted=1, updated=1, unchanged=1)
        assert w.upsert_prices([price(sid, D(2026, 1, 4), 3.0)] * 3) == UpsertResult(0, 0, unchanged=1, duplicates=2)
    assert con.execute("SELECT count(*) FROM price_daily").fetchone()[0] == 3


def test_unchanged_rows_are_not_rewritten_and_keep_their_first_source(db_path, con):
    with access.writer(db_path, "t") as w:
        sid = w.upsert_security(SecurityRecord(ticker="AAA"))
        s1 = w.record_source(SourceRecord(provider="cboe", dataset="equity.historical"))
        s2 = w.record_source(SourceRecord(provider="cboe", dataset="equity.historical"))
        w.upsert_prices([price(sid, D(2026, 1, 2), 1.0, s1)])
        result = w.upsert_prices([price(sid, D(2026, 1, 2), 1.0, s2)])  # same value, later retrieval
        assert result == UpsertResult(0, 0, unchanged=1)
    assert con.execute("SELECT source_id FROM price_daily").fetchone() == (s1,)
    assert con.execute("SELECT count(*) FROM sources").fetchone()[0] == 2  # the later retrieval is still recorded


def test_a_changed_value_takes_the_new_source(db_path, con):
    with access.writer(db_path, "t") as w:
        sid = w.upsert_security(SecurityRecord(ticker="AAA"))
        w.upsert_prices([price(sid, D(2026, 1, 2), 1.0, None)])
        s2 = w.record_source(SourceRecord(provider="cboe", dataset="equity.historical"))
        assert w.upsert_prices([price(sid, D(2026, 1, 2), 9.0, s2)]) == UpsertResult(0, 1)
    assert con.execute("SELECT close, source_id FROM price_daily").fetchone() == (9.0, s2)


def test_source_record_stores_command_parameters_version_as_of_and_fallback(db_path, con):
    with access.writer(db_path, "t") as w:
        sid = w.record_source(
            SourceRecord(
                provider="nasdaq", dataset="equity.historical", command="obb.nasdaq.equity.historical",
                parameters='{"symbol": "BRK.B"}', provider_version="openbb-nasdaq 2.0.0",
                as_of=dt.datetime(2026, 10, 1, 20, 0), is_fallback=True,
            )
        )  # fmt: skip
    row = con.execute(
        "SELECT command, parameters, provider_version, as_of, is_fallback FROM sources WHERE source_id = ?", [sid]
    ).fetchone()
    assert row == (
        "obb.nasdaq.equity.historical", '{"symbol": "BRK.B"}', "openbb-nasdaq 2.0.0", dt.datetime(2026, 10, 1, 20), True
    )  # fmt: skip


def test_frame_is_an_attribute_not_part_of_the_key(db_path, con):
    base = dict(
        cik=1, taxonomy="us-gaap", concept="Revenues", unit="USD", value=1.0, period_start=D(2025, 1, 1),
        period_end=D(2025, 12, 31), accession_no="0000000001-26-000001",
    )  # fmt: skip
    with access.writer(db_path, "t") as w:
        w.upsert_financial_facts([FinancialFactRecord(**base)])
        result = w.upsert_financial_facts([FinancialFactRecord(**base, frame="CY2025")])
    assert result == UpsertResult(0, 1)
    assert con.execute("SELECT count(*), max(frame) FROM financial_facts").fetchone() == (1, "CY2025")


def test_security_classification_sources_are_stored_and_never_erased_by_a_missing_value(db_path, con):
    with access.writer(db_path, "t") as w:
        w.upsert_security(SecurityRecord(ticker="QCOM", cik=804328, sic="3663", sic_source="sec"))
        w.upsert_security(
            SecurityRecord(ticker="QCOM", cik=804328, sector="Technology", industry="Semis", sector_source="nasdaq")
        )
        w.upsert_security(SecurityRecord(ticker="QCOM", cik=804328))
    assert con.execute("SELECT sector, industry, sector_source, sic, sic_source FROM securities").fetchall() == [
        ("Technology", "Semis", "nasdaq", "3663", "sec")
    ]


def test_market_quote_is_one_row_per_listing_and_provider_day(db_path, con):
    with access.writer(db_path, "t") as w:
        sid = w.upsert_security(SecurityRecord(ticker="GOOGL"))
        quote = MarketQuoteRecord(security_id=sid, quote_date=D(2026, 10, 1), last_price=338.24, market_cap=4.1e12)
        assert w.upsert_market_quotes([quote]) == UpsertResult(1, 0)
        assert w.upsert_market_quotes([quote]) == UpsertResult(0, 0, unchanged=1)
        assert w.upsert_market_quotes([quote.model_copy(update={"market_cap": 4.2e12})]) == UpsertResult(0, 1)
    assert con.execute("SELECT count(*) FROM market_quotes").fetchone()[0] == 1


def test_ownership_transaction_fields_round_trip_and_are_validated(db_path, con):
    rec = dict(
        cik=320193, holder_type="insider", holder_name="Doe Jane", as_of_date=D(2026, 1, 5), line_no=1,
        transaction_price=250.5, shares_owned_after=1000.0, acquired_disposed="D", is_derivative=False,
        security_title="Common Stock", ownership_nature="D", is_10b5_1=None,
    )  # fmt: skip
    with access.writer(db_path, "t") as w:
        w.upsert_ownership([OwnershipRecord(**rec)])
    assert con.execute(
        "SELECT transaction_price, acquired_disposed, is_derivative, ownership_nature, is_10b5_1 FROM ownership"
    ).fetchone() == (250.5, "D", False, "D", None)
    with pytest.raises(ValidationError):
        OwnershipRecord(**{**rec, "acquired_disposed": "X"})
