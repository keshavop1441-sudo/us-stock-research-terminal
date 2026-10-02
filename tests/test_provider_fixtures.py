"""Facts about SEC and Cboe payloads, pinned against REAL recorded responses.

The fixtures in tests/fixtures/provider_samples were captured by the OpenBB project's own test suite (see
PROVENANCE.json), trimmed, and committed here. They are evidence of payload SHAPE and semantics at the recorded
date. They are not live data and say nothing about what the providers return today.
"""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SAMPLES = Path(__file__).resolve().parent / "fixtures" / "provider_samples"


def load_json(name: str):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def test_every_fixture_has_provenance():
    provenance = load_json("PROVENANCE.json")
    files = {p.name for p in SAMPLES.iterdir() if p.name != "PROVENANCE.json"}
    assert files == set(provenance), "every sample needs a provenance entry and vice versa"
    for name, entry in provenance.items():
        assert re.fullmatch(r"[0-9a-f]{40}", entry["upstream_commit"]), name
        assert entry["upstream_repo"] == "https://github.com/OpenBB-finance/OpenBB"
        assert entry["recorded_http_date"] and entry["status"] == 200, name
        assert "NOT fetched by this project" in entry["origin"], name


# --- SEC company_tickers.json: the current ticker -> CIK map ---


@pytest.fixture(scope="module")
def tickers():
    data = load_json("company_tickers_sample.json")
    size = data.pop("_full_size")
    by_ticker: dict[str, list[dict]] = {}
    for entry in data.values():
        by_ticker.setdefault(entry["ticker"], []).append(entry)
    return size, by_ticker


def test_company_tickers_shape_cik_is_an_integer_not_a_padded_string(tickers):
    size, by_ticker = tickers
    assert size > 10_000
    entry = by_ticker["AAPL"][0]
    assert set(entry) == {"cik_str", "ticker", "title"}
    assert entry["cik_str"] == 320193 and isinstance(entry["cik_str"], int)  # must go through normalize_cik


def test_share_classes_share_one_cik_and_berkshire_uses_a_hyphen(tickers):
    _, by_ticker = tickers
    assert by_ticker["GOOG"][0]["cik_str"] == by_ticker["GOOGL"][0]["cik_str"] == 1652044
    assert by_ticker["BRK-B"][0]["cik_str"] == by_ticker["BRK-A"][0]["cik_str"] == 1067983
    assert "BRK.B" not in by_ticker  # SEC spelling is BRK-B; Nasdaq/Cboe use BRK.B (see docs/data_coverage.yaml)


def test_the_ticker_map_is_current_only_and_has_no_history(tickers):
    _, by_ticker = tickers
    assert "FB" not in by_ticker  # Facebook's old ticker: the issuer is simply listed as META
    assert by_ticker["META"][0]["cik_str"] == 1326801
    assert "TWTR" not in by_ticker  # delisted tickers vanish


# --- SEC submissions API ---

RECENT_COLUMNS = [
    "accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "act", "form", "fileNumber", "filmNumber",
    "items", "size", "isXBRL", "isInlineXBRL", "primaryDocument", "primaryDocDescription",
]  # fmt: skip


@pytest.fixture(scope="module")
def submissions():
    return load_json("submissions_aapl_trimmed.json")


def test_submissions_identity_fields(submissions):
    assert submissions["cik"] == "320193"  # NOT zero-padded here, a string
    assert submissions["tickers"] == ["AAPL"] and submissions["exchanges"] == ["Nasdaq"]
    assert submissions["sic"] == "3571" and submissions["fiscalYearEnd"] == "0928"
    assert submissions["entityType"] == "operating"


def test_submissions_former_names_carry_dates_but_no_tickers(submissions):
    assert submissions["formerNames"]
    assert all(set(n) == {"name", "from", "to"} for n in submissions["formerNames"])  # name history only
    assert not any("ticker" in n for n in submissions["formerNames"])


def test_submissions_filings_are_column_arrays_with_filing_acceptance_and_report_dates(submissions):
    recent = submissions["filings"]["recent"]
    assert list(recent) == RECENT_COLUMNS
    assert len({len(v) for v in recent.values()}) == 1, "columns must be aligned"
    assert all(re.fullmatch(r"\d{10}-\d{2}-\d{6}", a) for a in recent["accessionNumber"])
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", t) for t in recent["acceptanceDateTime"])
    assert {"4", "8-K", "10-K", "10-Q"} <= set(recent["form"])


def test_older_filings_live_in_separate_paginated_files(submissions):
    pages = submissions["filings"]["files"]
    assert pages and pages[0]["name"].endswith("-submissions-001.json") and pages[0]["filingFrom"] < "2000-01-01"


def test_for_form_4_the_report_date_is_the_transaction_period_not_the_filing_date(submissions):
    recent = submissions["filings"]["recent"]
    index = recent["form"].index("4")
    assert recent["reportDate"][index] < recent["filingDate"][index]
    assert recent["primaryDocument"][index].startswith("xslF345X")  # the HTML rendering; the raw XML sits one level up


# --- XBRL frames ---


def test_frames_are_one_concept_for_every_filer_in_one_calendar_period():
    frames = load_json("frames_sample.json")
    assert {"taxonomy", "tag", "ccp", "uom", "pts", "data"} <= set(frames)
    assert frames["ccp"] == "CY2023" and frames["uom"] == "USD" and frames["pts"] > 1000
    row = frames["data"][0]
    assert set(row) == {"accn", "cik", "entityName", "loc", "start", "end", "val"}
    assert isinstance(row["cik"], int)  # again an integer CIK
    assert row["accn"].count("-") == 2  # carries the accession of the filing the value came from


# --- Form 4 ownership XML ---


def parse(name: str) -> ET.Element:
    return ET.fromstring((SAMPLES / name).read_text(encoding="utf-8"))  # noqa: S314 - a committed fixture


def test_form4_carries_raw_transaction_codes_and_issuer_owner_identity():
    sales = parse("form4_sales_and_gift_aapl.xml")
    codes = [c.text for c in sales.iter("transactionCode")]
    assert codes == ["S", "S", "G"]  # open-market sales and a gift: not economically equivalent
    assert sales.findtext("./issuer/issuerCik") == "0000320193"  # padded here, unlike submissions
    assert sales.findtext("./issuer/issuerTradingSymbol") == "AAPL"
    owner_cik = sales.findtext("./reportingOwner/reportingOwnerId/rptOwnerCik")
    assert re.fullmatch(r"\d{10}", owner_cik) and owner_cik != "0000320193"  # a person's CIK, padded, not the issuer's
    assert sales.findtext("./reportingOwner/reportingOwnerRelationship/officerTitle")
    assert sales.findtext("./aff10b5One") in {"0", "1"}  # the 10b5-1 plan flag is part of the document


def test_form4_separates_non_derivative_sales_from_derivative_awards():
    sales, grants = parse("form4_sales_and_gift_aapl.xml"), parse("form4_derivative_grant_aapl.xml")
    assert len(list(sales.iter("nonDerivativeTransaction"))) == 3 and not list(sales.iter("derivativeTransaction"))
    assert len(list(grants.iter("derivativeTransaction"))) == 2 and not list(grants.iter("nonDerivativeTransaction"))
    assert {c.text for c in grants.iter("transactionCode")} == {"A"}  # RSU awards: compensation, not a market signal


def test_form4_sale_lines_have_shares_price_and_post_transaction_holdings():
    sale = next(parse("form4_sales_and_gift_aapl.xml").iter("nonDerivativeTransaction"))
    assert float(sale.findtext(".//transactionShares/value")) > 0
    assert float(sale.findtext(".//transactionPricePerShare/value")) > 0
    assert float(sale.findtext(".//sharesOwnedFollowingTransaction/value")) >= 0
    assert sale.findtext(".//transactionAcquiredDisposedCode/value") == "D"
    assert sale.findtext(".//directOrIndirectOwnership/value") in {"D", "I"}


# --- 13F ---


def test_a_13f_belongs_to_the_filer_not_to_the_issuers_it_holds():
    header = (SAMPLES / "form13f_nvidia_header.txt").read_text(encoding="utf-8")
    assert re.search(r"CONFORMED SUBMISSION TYPE:\s+13F-HR", header)
    assert re.search(r"CONFORMED PERIOD OF REPORT:\s+20240331", header)  # quarter-end, filed ~45 days later
    assert re.search(r"FILED AS OF DATE:\s+20240515", header)
    assert re.search(r"CENTRAL INDEX KEY:\s+0001045810", header)  # the FILER's CIK (here NVIDIA as an investor)


# --- Cboe daily history ---


def test_cboe_history_is_split_adjusted_back_to_2004():
    data = load_json("cboe_historical_aapl_sample.json")
    rows = {r["date"]: r for r in data["data"]}
    assert data["symbol"] == "AAPL" and data["_full_rows"] > 5000 and "2004-01-02" in rows
    assert rows["2004-01-02"]["close"] < 1.0  # AAPL traded around $21 then; 2:1, 7:1 and 4:1 splits since => ~$0.38
    for before, after in (("2005-02-25", "2005-02-28"), ("2014-06-06", "2014-06-09"), ("2020-08-28", "2020-08-31")):
        jump = rows[after]["close"] / rows[before]["close"]
        assert 0.8 < jump < 1.3, f"unadjusted split would show a 2x/7x/4x move, saw {jump:.2f}x across {before}"
    assert (
        rows["2020-08-31"]["volume"] > 100_000_000
    )  # split-scaled volume too (raw AAPL volume that week was ~40M x 4)


def test_cboe_daily_rows_have_plain_date_strings_and_no_timezone():
    row = load_json("cboe_historical_aapl_sample.json")["data"][0]
    assert set(row) == {"date", "volume", "open", "high", "low", "close"}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"])
