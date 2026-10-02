"""The Phase 1 evidence model (``sources`` + record keys), exercised with the shapes the Phase 2 audit found.

Nothing here loads market data: records are built in the test from audit evidence, written to a temp database, and read
back. The tests pin what the CURRENT model can and cannot represent so the gaps listed in docs/data_coverage.yaml
(``source_evidence_model``) stay honest.
"""

import datetime as dt
import json

import pytest
from pydantic import ValidationError

from app.database import access
from app.models.records import (
    EventRecord,
    FilingRecord,
    FinancialFactRecord,
    OwnershipRecord,
    SecurityRecord,
    SourceRecord,
)
from app.models.symbols import canonical_symbol
from app.screening import metrics as m
from app.screening.metrics import FactPoint

D = dt.date


def count(con, table):
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# --- source metadata normalisation ---------------------------------------------------------------------------------
SOURCE_SHAPES = {
    "sec_filing": dict(
        provider="sec",
        dataset="company_filings",
        url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm",
        detail={"command": "obb.sec.company_filings", "params": {"symbol": "AAPL", "form_type": "10-K"}},
    ),
    "market_data": dict(
        provider="cboe",
        dataset="equity.historical",
        url=None,
        detail={"command": "obb.cboe.equity.historical", "params": {"symbol": "BRK.B"}, "adjusted": "split-only"},
    ),
    "news": dict(
        provider="nasdaq",
        dataset="news.company",
        url="https://www.nasdaq.com/articles/example",
        detail={"command": "obb.news.company", "params": {"symbol": "AAPL"}, "published": "2026-10-01"},
    ),
    "government_award": dict(
        provider="usaspending",
        dataset="award_detail",
        url="https://api.usaspending.gov/api/v2/awards/CONT_AWD_DEAC0494AL85000_8900_-NONE-_-NONE-/",
        detail={"command": "GET /api/v2/awards/{generated_unique_award_id}/", "recipient_uei": "FYHNA5WC8XD7"},
    ),
}


@pytest.mark.parametrize("kind", SOURCE_SHAPES)
def test_each_audited_source_type_fits_a_source_record_with_command_and_params_in_detail(kind, db_path, con):
    shape = SOURCE_SHAPES[kind]
    record = SourceRecord(
        provider=f" {shape['provider'].upper()} ".lower(),
        dataset=shape["dataset"],
        url=shape["url"],
        content_hash="sha256:" + "0" * 64,
        detail=json.dumps(shape["detail"], sort_keys=True),
    )
    with access.writer(db_path, "test") as w:
        source_id = w.record_source(record)
    row = con.execute(
        "SELECT provider, dataset, url, detail, retrieved_at FROM sources WHERE source_id = ?", [source_id]
    ).fetchone()
    assert row[0] == shape["provider"] and row[1] == shape["dataset"] and row[2] == shape["url"]
    assert json.loads(row[3])["command"], "the command/endpoint must be recoverable from the stored evidence"
    assert row[4] is not None and row[4].tzinfo is None  # retrieval time is stored as naive UTC


def test_source_record_rejects_unusable_identity_text():
    with pytest.raises(ValidationError):
        SourceRecord(provider="  ", dataset="x")
    with pytest.raises(ValidationError):
        SourceRecord(provider="sec", dataset="a|b")  # '|' separates business keys
    with pytest.raises(ValidationError):
        SourceRecord(provider="sec", dataset="x", unexpected="field")  # extra fields forbidden


def test_two_retrievals_of_the_same_dataset_are_two_evidence_rows_by_design(db_path, con):
    """Provenance is append-only: the same fetch twice is two events (they can have different content)."""
    record = SourceRecord(provider="sec", dataset="submissions", content_hash="sha256:abc")
    with access.writer(db_path, "test") as w:
        first, second = w.record_source(record), w.record_source(record)
    assert first != second and count(con, "sources") == 2


def test_source_record_has_structured_command_params_version_as_of_and_fallback_flag():
    """Gap S1-S3 proven by the audit (see data_coverage.yaml), closed by schema v3."""
    assert {"command", "parameters", "provider_version", "as_of", "is_fallback"} <= set(SourceRecord.model_fields)  # v3


# --- duplicate logical records and restatements ------------------------------------------------------------------
def fact(value, accession, filed_form="10-K", **overrides):
    values = dict(
        cik=1874178, taxonomy="us-gaap", concept="NetIncomeLoss", unit="USD", value=value,
        period_start=D(2024, 1, 1), period_end=D(2024, 12, 31), accession_no=accession,
        fiscal_year=2024, fiscal_period="FY", form=filed_form, filed_date=D(2025, 2, 24),
    )  # fmt: skip
    return FinancialFactRecord(**{**values, **overrides})


def test_restated_period_keeps_both_vintages_and_point_in_time_picks_the_right_one(db_path, con):
    original = fact(-4747e6, "0001874178-25-000010", filed_date=D(2025, 2, 24))
    restated = fact(-4700e6, "0001874178-26-000008", filed_date=D(2026, 2, 12))  # later filing; value is illustrative
    with access.writer(db_path, "test") as w:
        w.upsert_financial_facts([original, restated])
        w.upsert_financial_facts([original, restated])  # reload: still two rows
    assert count(con, "financial_facts") == 2
    rows = con.execute("SELECT value, accession_no, filed_date FROM financial_facts ORDER BY filed_date").fetchall()
    points = [FactPoint(v, D(2024, 1, 1), D(2024, 12, 31), filed, accn, "10-K") for v, accn, filed in rows]
    assert m.was_restated(points)
    assert m.latest_known(points).value == -4700e6  # today's view
    assert m.latest_known(points, as_of=D(2025, 6, 30)).value == -4747e6  # what a reader had in mid-2025
    assert m.latest_known(points, as_of=D(2025, 1, 1)) is None  # before the first filing nothing was known


def test_same_fact_from_the_same_filing_twice_is_one_row_even_when_the_value_is_refreshed(db_path, con):
    with access.writer(db_path, "test") as w:
        w.upsert_financial_facts([fact(-1.0, "0001-25-1")])
        result = w.upsert_financial_facts([fact(-2.0, "0001-25-1")])  # provider corrected its payload
    assert (result.inserted, result.updated, result.unchanged) == (0, 1, 0)
    assert con.execute("SELECT value FROM financial_facts").fetchall() == [(-2.0,)]


def test_original_and_amended_filings_are_distinct_logical_records(db_path, con):
    """AMD filed a 10-K/A on 2026-02-04 (accession 0000002488-26-000021, audit scan); the original is another filing."""
    amendment = FilingRecord(
        accession_no="0000002488-26-000021",
        cik=2488,
        form="10-K/A",
        filing_date=D(2026, 2, 4),
        report_date=D(2025, 12, 27),
    )
    original = FilingRecord(  # accession illustrative: the audit did not record the original's number
        accession_no="0000002488-26-000010",
        cik="0000002488",
        form="10-K",
        filing_date=D(2026, 1, 28),
        report_date=D(2025, 12, 27),
    )
    with access.writer(db_path, "test") as w:
        w.upsert_filings([original, amendment])
        w.upsert_filings([amendment])
    assert count(con, "filings") == 2
    forms = dict(con.execute("SELECT form, accession_no FROM filings").fetchall())
    assert set(forms) == {"10-K", "10-K/A"}
    assert all(f.endswith("/A") == (f == "10-K/A") for f in forms)  # amendment status is derivable from the form code


def test_amendment_status_is_derived_from_the_form_code_for_every_form_family_seen_in_the_audit():
    amended = ["10-K/A", "10-Q/A", "8-K/A", "SCHEDULE 13G/A", "SC 13G/A", "144/A"]
    originals = ["10-K", "10-Q", "8-K", "4", "3", "5", "13F-HR", "SCHEDULE 13G", "144"]
    assert all(form.endswith("/A") for form in amended) and not any(form.endswith("/A") for form in originals)


# --- share classes: one issuer, several listed securities ------------------------------------------------------------
def test_share_classes_are_separate_securities_of_one_issuer_and_spellings_collapse_before_writing(db_path, con):
    with access.writer(db_path, "test") as w:
        googl = w.upsert_security(SecurityRecord(ticker="GOOGL", cik=1652044, name="Alphabet Inc."))
        goog = w.upsert_security(SecurityRecord(ticker="GOOG", cik=1652044, name="Alphabet Inc."))
        # Nasdaq/Cboe spell the class suffix with a dot, SEC with a dash: callers canonicalise first
        brk_b = w.upsert_security(SecurityRecord(ticker=canonical_symbol("BRK.B"), cik=1067983))
        again = w.upsert_security(SecurityRecord(ticker=canonical_symbol("BRK-B"), cik="0001067983"))
    assert googl != goog and brk_b == again
    assert count(con, "securities") == 3
    assert con.execute("SELECT count(DISTINCT cik) FROM securities").fetchone()[0] == 2  # Alphabet, Berkshire
    # the raw normaliser deliberately does not reconcile spellings (identity is the caller's responsibility)
    with access.writer(db_path, "test") as w:
        assert w.upsert_security(SecurityRecord(ticker="BRK.B", cik=1067983)) != brk_b


def test_a_discontinued_ticker_can_be_kept_inactive_under_its_cik(db_path, con):
    with access.writer(db_path, "test") as w:
        w.upsert_security(SecurityRecord(ticker="TWTR", cik=1418091, name="Twitter, Inc.", is_active=True))
        w.upsert_security(SecurityRecord(ticker="TWTR", cik=1418091, is_active=False))  # acquired 2022
    assert con.execute("SELECT ticker, cik, is_active FROM securities").fetchall() == [("TWTR", "0001418091", False)]


# --- insider rows: identical-looking lines are different records ------------------------------------------------------
def test_identical_insider_lines_in_one_filing_need_line_numbers_to_survive(db_path, con):
    """The audit saw two Form 4 rows with identical date, shares and price in one AAPL filing (RSU grants)."""
    base = dict(
        cik=320193, holder_type="insider", holder_name="Example Officer", as_of_date=D(2026, 9, 27),
        accession_no="0001140360-26-038028", shares=47645.0, transaction_code="A", form="4",
    )  # fmt: skip
    first, second = OwnershipRecord(**base, line_no=0), OwnershipRecord(**base, line_no=1)
    assert first.key != second.key
    with access.writer(db_path, "test") as w:
        w.upsert_ownership([first, second])
        w.upsert_ownership([first, second])
    assert count(con, "ownership") == 2
    collapsed = OwnershipRecord(**base)  # line_no defaults to 0: a loader that forgets it would drop the second line
    assert collapsed.key == first.key


def test_ownership_represents_price_and_post_transaction_holdings_since_v3():
    """Gap G1 proven by the audit (Form 4 rows carry price, securities_owned, A/D, derivative flag); closed in v3."""
    fields = set(OwnershipRecord.model_fields)
    assert fields >= {"transaction_price", "shares_owned_after", "acquired_disposed", "is_derivative", "is_10b5_1"}


# --- news / events: stable references dedupe across providers ----------------------------------------------------
def test_same_article_url_from_two_retrievals_is_one_event(db_path, con):
    event = EventRecord(
        cik=320193, event_type="news", source_ref="https://www.nasdaq.com/articles/example", title="Headline",
        event_date=D(2026, 10, 1),
    )  # fmt: skip
    refreshed = event.model_copy(update={"summary": "excerpt added later"})
    with access.writer(db_path, "test") as w:
        w.upsert_events([event])
        result = w.upsert_events([refreshed])
    assert count(con, "events") == 1 and (result.inserted, result.updated) == (0, 1)
