"""SEC JSON -> records (SYNTHETIC documents): identity, filings index, companyfacts allow-list and rejects."""

from datetime import date

import p0_synthetic as syn
import pytest

from app.ingestion.errors import IdentityError, MalformedResponseError
from app.ingestion.sec_normalize import parse_companyfacts, parse_submissions, parse_ticker_map, resolve

AAPL = "0000320193"


def test_ticker_map_resolves_one_cik_and_normalises_it_in_one_place():
    mapping = parse_ticker_map(syn.ticker_map())
    assert resolve(mapping, "aapl").cik == AAPL
    assert (
        resolve(mapping, "BRK-B").cik == "0001067983" and resolve(mapping, "GOOG").cik == resolve(mapping, "GOOGL").cik
    )


def test_provider_spellings_of_a_class_ticker_resolve_to_the_sec_entry():
    mapping = parse_ticker_map(syn.ticker_map())
    assert resolve(mapping, "BRK.B").cik == "0001067983"  # Nasdaq/Cboe spelling


def test_missing_ambiguous_and_contradicted_identities_are_errors_not_guesses():
    mapping = parse_ticker_map(syn.ticker_map(("AAPL",)))
    with pytest.raises(IdentityError, match="not in the SEC ticker map"):
        resolve(mapping, "NOPE")
    clash = {"fields": ["cik", "name", "ticker", "exchange"], "data": [[1, "A", "ZZZ", None], [2, "B", "ZZZ", None]]}
    with pytest.raises(IdentityError, match="ambiguous"):
        resolve(parse_ticker_map(clash), "ZZZ")
    with pytest.raises(IdentityError, match="audit recorded"):
        resolve(mapping, "AAPL", expected_cik="0000000001")


def test_malformed_ticker_map_is_rejected_but_a_bad_row_only_loses_itself():
    with pytest.raises(MalformedResponseError):
        parse_ticker_map({"fields": ["x"], "data": []})
    data = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [["junk", "X", "BAD", None], [320193, "Apple", "AAPL", "Nasdaq"]],
    }
    assert list(parse_ticker_map(data)) == ["AAPL"]


def test_submissions_give_profile_and_filings_with_amendments_kept_distinct():
    doc = syn.submissions(
        320193,
        "AAPL",
        forms=(("10-K", "2025-10-31", "2025-09-27"), ("10-K/A", "2026-02-04", "2025-09-27"), ("8-K", "2026-01-01", "")),
    )
    profile, filings = parse_submissions(doc, AAPL)
    assert (
        profile.cik == AAPL
        and profile.sic == "3571"
        and profile.fiscal_year_end == (9, 26)
        and profile.tickers == ("AAPL",)
    )
    assert [(f.form, f.filing_date) for f in filings] == [
        ("10-K", date(2025, 10, 31)),
        ("10-K/A", date(2026, 2, 4)),
    ]  # 8-K not indexed
    assert len({f.accession_no for f in filings}) == 2 and filings[1].url.endswith("doc.htm")
    assert filings[0].url.startswith("https://www.sec.gov/Archives/edgar/data/320193/")


def test_submissions_for_a_different_cik_or_with_ragged_arrays_are_rejected():
    with pytest.raises(MalformedResponseError, match="asked for CIK"):
        parse_submissions(syn.submissions(789019, "MSFT"), AAPL)
    doc = syn.submissions(320193, "AAPL")
    doc["filings"]["recent"]["form"].pop()
    with pytest.raises(MalformedResponseError, match="different lengths"):
        parse_submissions(doc, AAPL)


def test_a_malformed_fiscal_year_end_stays_unknown_instead_of_failing_identity():
    doc = syn.submissions(320193, "AAPL")
    doc["fiscalYearEnd"] = "13/45"
    assert parse_submissions(doc, AAPL)[0].fiscal_year_end is None


def test_companyfacts_keeps_only_allow_listed_concepts_forms_and_units_and_counts_the_rest():
    doc = syn.standard_issuer()
    b = syn.FactsBuilder(320193)
    b.facts = doc["facts"]
    b.add(
        "Revenues", "EUR", date(2024, 1, 1), date(2024, 12, 31), 1.0, "0000000001-25-000001", date(2025, 1, 1), "10-K"
    )
    b.add("RevenueFromContractWithCustomerExcludingAssessedTax", "USD", date(2024, 9, 29), date(2025, 9, 27), 1.0,
          "0000000001-25-000002", date(2025, 11, 1), "S-1")  # fmt: skip
    b.add("SomethingElse", "USD", None, date(2025, 9, 27), 1.0, "0000000001-25-000003", date(2025, 11, 1), "10-K")
    parsed = parse_companyfacts(b.build(), AAPL)
    assert "SomethingElse" not in {p.concept for p in parsed.points}
    assert parsed.skipped["UNEXPECTED_UNIT:EUR"] == 1 and parsed.skipped["FORM:S-1"] == 1
    assert all(p.accession and p.form in {"10-K", "10-Q"} for p in parsed.points)
    assert parsed.points[0].cik == AAPL


def test_a_point_without_provenance_or_with_schema_drift_is_rejected_and_reported_not_stored():
    doc = syn.standard_issuer(q1=False)
    points = doc["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
    points.append(
        {"end": "2025-09-27", "start": "2024-09-29", "val": 1.0, "form": "10-K", "filed": "2025-10-31"}
    )  # no accn
    points.append({**points[0], "brand_new_sec_field": 1})
    parsed = parse_companyfacts(doc, AAPL)
    reasons = " ".join(str(r["error"]) for r in parsed.rejects)
    assert "MISSING_PROVENANCE" in reasons and "unexpected companyfacts keys" in reasons and len(parsed.rejects) == 2


def test_duplicate_logical_keys_inside_a_document_are_counted():
    doc = syn.standard_issuer(q1=False)
    points = doc["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
    points.append(dict(points[0]))
    assert parse_companyfacts(doc, AAPL).duplicates == 1


def test_frame_and_instants_are_preserved():
    b = syn.FactsBuilder(320193)
    b.add(
        "StockholdersEquity",
        "USD",
        None,
        date(2025, 9, 27),
        5.0,
        "0000000001-25-000100",
        date(2025, 10, 31),
        "10-K",
        2025,
        "FY",
        frame="CY2025Q3I",
    )
    point = parse_companyfacts(b.build(), AAPL).points[0]
    assert point.is_instant and point.frame == "CY2025Q3I" and point.to_record().frame == "CY2025Q3I"


def test_foreign_issuer_with_only_ifrs_facts_yields_no_points_but_names_its_taxonomy():
    parsed = parse_companyfacts({"cik": 1046179, "facts": {"ifrs-full": {}}}, "0001046179")
    assert parsed.points == [] and parsed.taxonomies == ("ifrs-full",)


def test_companyfacts_for_another_cik_is_rejected():
    with pytest.raises(MalformedResponseError, match="asked for CIK"):
        parse_companyfacts({"cik": 1, "facts": {}}, AAPL)
    with pytest.raises(MalformedResponseError, match="no 'facts'"):
        parse_companyfacts({"cik": 320193}, AAPL)
