"""Idempotent ingestion: loading the same logical record twice must never create two rows."""

import datetime as dt

import pytest
from pydantic import ValidationError

from app.database import access
from app.database.write_repository import AmbiguousSecurityError, UpsertResult
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

D = dt.date


def count(con, table):
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def fact(**overrides):
    values = {
        "cik": 320193, "taxonomy": "us-gaap", "concept": "Revenues", "unit": "USD", "value": 100.0,
        "period_start": D(2023, 1, 1), "period_end": D(2023, 12, 31), "accession_no": "0000320193-24-000001",
        "fiscal_year": 2023, "fiscal_period": "FY", "form": "10-K",
    }  # fmt: skip
    return FinancialFactRecord(**{**values, **overrides})


# --- securities: identity (ticker, cik) ------------------------------------------------------


def test_security_upsert_is_idempotent_and_id_is_stable(db_path, con):
    with access.writer(db_path, "test") as w:
        first = w.upsert_security(SecurityRecord(ticker="aapl", cik=320193, name="Apple Inc."))
        second = w.upsert_security(SecurityRecord(ticker=" AAPL ", cik="0000320193", exchange="NASDAQ"))
    assert first == second
    assert count(con, "securities") == 1
    assert con.execute("SELECT cik, ticker, name, exchange FROM securities").fetchall() == [
        ("0000320193", "AAPL", "Apple Inc.", "NASDAQ")
    ]


def test_security_reload_with_missing_attributes_does_not_erase_known_values(db_path, con):
    with access.writer(db_path, "test") as w:
        w.upsert_security(
            SecurityRecord(ticker="AAPL", cik=320193, name="Apple Inc.", sector="Technology", is_active=True)
        )
        w.upsert_security(SecurityRecord(ticker="AAPL", cik=320193))  # a source that knows nothing else
    assert con.execute("SELECT name, sector, is_active FROM securities").fetchall() == [
        ("Apple Inc.", "Technology", True)
    ]


def test_security_learns_its_cik_later_instead_of_duplicating(db_path, con):
    with access.writer(db_path, "test") as w:
        first = w.upsert_security(SecurityRecord(ticker="MSFT"))  # e.g. from a price feed: no CIK known
        second = w.upsert_security(SecurityRecord(ticker="MSFT", cik=789019))
        third = w.upsert_security(SecurityRecord(ticker="MSFT"))  # cik-less source refers to the same security
    assert first == second == third
    assert con.execute("SELECT cik FROM securities").fetchall() == [("0000789019",)]


def test_share_classes_with_one_cik_are_two_securities(db_path, con):
    with access.writer(db_path, "test") as w:
        goog = w.upsert_security(SecurityRecord(ticker="GOOG", cik=1652044))
        googl = w.upsert_security(SecurityRecord(ticker="GOOGL", cik=1652044))
        assert goog != googl
        assert w.upsert_security(SecurityRecord(ticker="GOOGL", cik=1652044)) == googl
    assert count(con, "securities") == 2


def test_reused_ticker_under_another_issuer_is_a_different_security(db_path, con):
    with access.writer(db_path, "test") as w:
        old = w.upsert_security(SecurityRecord(ticker="XYZ", cik=1))
        new = w.upsert_security(SecurityRecord(ticker="XYZ", cik=2))
        with pytest.raises(AmbiguousSecurityError):
            w.upsert_security(SecurityRecord(ticker="XYZ"))  # which one? refuse to guess
    assert old != new
    assert count(con, "securities") == 2


def test_security_update_works_while_price_rows_reference_it(db_path, con):
    with access.writer(db_path, "test") as w:
        sid = w.upsert_security(SecurityRecord(ticker="AAPL", cik=320193))
        w.upsert_prices([PriceRecord(security_id=sid, trade_date=D(2024, 1, 2), close=1.0)])
        w.upsert_security(SecurityRecord(ticker="AAPL", cik=320193, name="Apple Inc.", exchange="NASDAQ"))
    assert con.execute("SELECT name FROM securities WHERE security_id = ?", [sid]).fetchone() == ("Apple Inc.",)


# --- financial_facts -----------------------------------------------------------------------


def test_same_fact_twice_is_one_row_even_with_cik_spelled_differently(db_path, con):
    with access.writer(db_path, "test") as w:
        assert w.upsert_financial_facts([fact()]) == UpsertResult(inserted=1, updated=0)
        assert w.upsert_financial_facts([fact(cik="0000320193")]) == UpsertResult(inserted=0, updated=1)
        assert w.upsert_financial_facts([fact(cik=" 320193 ")]) == UpsertResult(inserted=0, updated=1)
    assert count(con, "financial_facts") == 1


def test_reloading_a_corrected_value_updates_in_place(db_path, con):
    with access.writer(db_path, "test") as w:
        w.upsert_financial_facts([fact(value=100.0)])
        w.upsert_financial_facts([fact(value=105.5, form="10-K/A")])
    assert con.execute("SELECT value, form FROM financial_facts").fetchall() == [(105.5, "10-K/A")]


def test_same_period_reported_in_a_later_filing_is_a_separate_as_reported_row(db_path, con):
    with access.writer(db_path, "test") as w:
        w.upsert_financial_facts(
            [fact(accession_no="0000320193-24-000001"), fact(accession_no="0000320193-25-000009", value=101.0)]
        )
    assert count(con, "financial_facts") == 2


def test_different_period_unit_or_concept_are_different_facts(db_path, con):
    with access.writer(db_path, "test") as w:
        w.upsert_financial_facts(
            [fact(), fact(period_end=D(2024, 12, 31)), fact(unit="EUR"), fact(concept="NetIncomeLoss")]
        )
    assert count(con, "financial_facts") == 4


def test_point_in_time_fact_without_period_start_or_accession_is_idempotent(db_path, con):
    """NULL components must not defeat de-duplication (a plain UNIQUE index would treat NULLs as distinct)."""
    instant = fact(concept="Assets", period_start=None, accession_no=None)
    with access.writer(db_path, "test") as w:
        w.upsert_financial_facts([instant])
        w.upsert_financial_facts([instant, instant])
    assert count(con, "financial_facts") == 1


def test_duplicate_keys_inside_one_batch_keep_the_last_value(db_path, con):
    with access.writer(db_path, "test") as w:
        result = w.upsert_financial_facts([fact(value=1.0), fact(value=2.0), fact(value=3.0)])
    assert result == UpsertResult(inserted=1, updated=0)
    assert con.execute("SELECT value FROM financial_facts").fetchone() == (3.0,)


def test_large_batch_is_chunked_and_idempotent(db_path, con):
    facts = [fact(period_end=D(2000, 1, 1) + dt.timedelta(days=i), period_start=None) for i in range(1234)]
    with access.writer(db_path, "test") as w:
        assert w.upsert_financial_facts(facts) == UpsertResult(inserted=1234, updated=0)
        assert w.upsert_financial_facts(facts) == UpsertResult(inserted=0, updated=1234)
    assert count(con, "financial_facts") == 1234


# --- filings, prices, earnings, ownership, events ------------------------------------------


def test_filing_upsert_by_accession_number(db_path, con):
    filing = FilingRecord(accession_no="0000320193-24-000001", cik=320193, form="10-K", filing_date=D(2024, 11, 1))
    with access.writer(db_path, "test") as w:
        w.upsert_filings([filing])
        w.upsert_filings([filing.model_copy(update={"description": "Annual report"})])
    assert con.execute("SELECT count(*), max(description) FROM filings").fetchone() == (1, "Annual report")


def test_price_upsert_by_security_and_date(db_path, con):
    with access.writer(db_path, "test") as w:
        sid = w.upsert_security(SecurityRecord(ticker="AAPL", cik=320193))
        bar = PriceRecord(security_id=sid, trade_date=D(2024, 1, 2), close=10.0, volume=5)
        w.upsert_prices([bar])
        w.upsert_prices(
            [bar.model_copy(update={"close": 11.0}), PriceRecord(security_id=sid, trade_date=D(2024, 1, 3), close=12.0)]
        )
    assert con.execute("SELECT trade_date, close FROM price_daily ORDER BY trade_date").fetchall() == [
        (D(2024, 1, 2), 11.0),
        (D(2024, 1, 3), 12.0),
    ]


def test_earnings_identity_is_the_fiscal_period_so_a_moved_date_updates_not_duplicates(db_path, con):
    announced = EarningsRecord(
        cik=320193, fiscal_year=2025, fiscal_period="Q1", report_date=D(2025, 2, 1), eps_estimate=1.5
    )
    with access.writer(db_path, "test") as w:
        w.upsert_earnings([announced])
        w.upsert_earnings([announced.model_copy(update={"report_date": D(2025, 2, 4), "eps_actual": 1.6})])
        w.upsert_earnings([announced.model_copy(update={"fiscal_period": "Q2"})])  # a different period
    assert con.execute(
        "SELECT fiscal_period, report_date, eps_actual FROM earnings ORDER BY fiscal_period"
    ).fetchall() == [
        ("Q1", D(2025, 2, 4), 1.6),
        ("Q2", D(2025, 2, 1), None),
    ]


def ownership(**overrides):
    values = {
        "cik": 320193, "holder_type": "insider", "holder_name": "Cook Timothy D", "as_of_date": D(2024, 4, 1),
        "accession_no": "0001-24-000005", "line_no": 1, "shares": 10.0,
    }  # fmt: skip
    return OwnershipRecord(**{**values, **overrides})


def test_ownership_same_filing_line_is_one_row_but_other_lines_and_holders_are_separate(db_path, con):
    with access.writer(db_path, "test") as w:
        w.upsert_ownership([ownership()])
        w.upsert_ownership(
            [ownership(holder_name="  COOK  timothy d", shares=12.0)]
        )  # same holder, spelled differently
    assert count(con, "ownership") == 1
    with access.writer(db_path, "test") as w:
        w.upsert_ownership(
            [ownership(line_no=2), ownership(holder_name="Someone Else"), ownership(holder_type="institution")]
        )
    assert count(con, "ownership") == 4
    assert con.execute(
        "SELECT shares FROM ownership WHERE line_no = 1 AND holder_type = 'insider' AND holder_key LIKE 'cook%'"
    ).fetchone() == (12.0,)


def test_holder_names_containing_the_key_separator_still_produce_valid_keys(db_path, con):
    with access.writer(db_path, "test") as w:
        w.upsert_ownership(
            [ownership(holder_name="Smith | Jones Partners"), ownership(holder_name="smith jones   partners")]
        )
    assert count(con, "ownership") == 1


def test_ownership_holder_cik_is_the_identity_when_known(db_path, con):
    with access.writer(db_path, "test") as w:
        w.upsert_ownership([ownership(holder_name="Vanguard Group", holder_cik=102909, holder_type="institution")])
        w.upsert_ownership(
            [ownership(holder_name="VANGUARD GROUP INC", holder_cik="0000102909", holder_type="institution")]
        )
    assert count(con, "ownership") == 1


def test_event_identity_is_issuer_type_and_source_reference(db_path, con):
    event = EventRecord(cik=320193, event_type="news", source_ref="https://example.com/a", title="Headline")
    with access.writer(db_path, "test") as w:
        w.upsert_events([event])
        w.upsert_events([event.model_copy(update={"title": "Edited headline"})])
        w.upsert_events([event.model_copy(update={"source_ref": "https://example.com/b"})])  # another article
        w.upsert_events([event.model_copy(update={"event_type": "lawsuit"})])  # same ref, other kind
        w.upsert_events(
            [EventRecord(event_type="news", source_ref="https://example.com/a", title="Not tied to an issuer")]
        )
    assert count(con, "events") == 4
    assert con.execute(
        "SELECT title FROM events WHERE source_ref = 'https://example.com/a' "
        "AND event_type = 'news' AND cik IS NOT NULL"
    ).fetchone() == ("Edited headline",)


def test_sources_are_append_only_provenance(db_path, con):
    with access.writer(db_path, "test") as w:
        first = w.record_source(SourceRecord(provider="sec", dataset="company_facts"))
        second = w.record_source(SourceRecord(provider="sec", dataset="company_facts"))
    assert first != second  # each retrieval is its own provenance record
    assert count(con, "sources") == 2


# --- validation and transactions ---------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda: fact(cik="abc"),
        lambda: fact(cik=10**10),
        lambda: fact(value=float("nan")),
        lambda: fact(value=float("inf")),
        lambda: fact(concept="a|b"),  # the key separator would make keys ambiguous
        lambda: fact(taxonomy="  "),
        lambda: SecurityRecord(ticker="not a ticker"),
        lambda: SecurityRecord(ticker="AAPL", cik=""),
        lambda: EventRecord(event_type="news", source_ref="", title="x"),
        lambda: ownership(line_no=-1),
        lambda: ownership(holder_name="   "),
        lambda: fact(unexpected_field=1),
    ],
)
def test_invalid_records_are_rejected_before_reaching_the_database(build):
    with pytest.raises((ValidationError, ValueError)):
        build()


def test_a_failing_write_rolls_back_the_whole_transaction(db_path, con):
    with pytest.raises(RuntimeError, match="boom"), access.writer(db_path, "test") as w:
        w.record_query("should vanish")
        w.upsert_financial_facts([fact()])
        raise RuntimeError("boom")
    assert count(con, "query_history") == 0
    assert count(con, "financial_facts") == 0


def test_a_database_constraint_failure_rolls_back_earlier_writes_in_the_same_session(db_path, con):
    import duckdb

    with pytest.raises(duckdb.ConstraintException), access.writer(db_path, "test") as w:
        w.record_query("should vanish")
        w.upsert_prices([PriceRecord(security_id=999, trade_date=D(2024, 1, 2))])  # no such security
    assert count(con, "query_history") == 0


def test_query_and_watchlist_validation(db_path):
    with access.writer(db_path, "test") as w:
        assert w.record_query("  tech stocks  ") == 1
        for bad in ("", "   ", "x" * 2001):
            with pytest.raises(ValueError):
                w.record_query(bad)
        w.create_watchlist(" Growth ", "g")
        for bad in ("growth", " ", ""):
            with pytest.raises(ValueError):
                w.create_watchlist(bad)


def test_queries_are_stored_literally_not_executed(db_path, con):
    with access.writer(db_path, "test") as w:
        w.record_query("'; DROP TABLE securities; --")
    assert con.execute("SELECT query_text FROM query_history").fetchone() == ("'; DROP TABLE securities; --",)
    assert count(con, "securities") == 0  # table still exists (count would raise otherwise)
