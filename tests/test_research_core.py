"""The pure research contracts: envelope, metric catalog, screen specification, screen evaluation, freshness,
filing-event classification. No database, no network."""

import json
import math
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from app.research import freshness as fr
from app.research.catalog import CATALOG, catalog_json
from app.research.envelope import make_envelope, metric_dict, to_jsonable
from app.research.evidence import (
    SECTIONS,
    Company,
    classify_submissions,
    filing_url,
    sections_for_filing,
)
from app.research.format import money, render
from app.research.screen_spec import Criterion, ScreenSpec
from app.research.screening import FAIL, MATCH, NO_MATCH, PASS, UNEVALUABLE, SecurityView, run_screen
from app.research.universe import MAX_INGEST_SYMBOLS, UniverseError, build_manifest, parse_stages, parse_symbols
from app.screening import classification as cls
from app.screening.metrics import MetricResult, MetricState

D = date

# --- envelope ---


def test_envelope_is_plain_json_with_a_stable_shape():
    env = make_envelope(
        "x",
        {"a": D(2026, 1, 2), "n": math.nan, "m": MetricResult(MetricState.OK, 1.5)},
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert set(env) == {"schema_version", "command", "status", "generated_at", "data", "warnings", "errors"}
    assert env["data"] == {
        "a": "2026-01-02",
        "n": None,
        "m": {"state": "OK", "value": 1.5, "reason": None, "flags": []},
    }
    json.dumps(env, allow_nan=False)


def test_envelope_rejects_unknown_status_and_unserialisable_objects():
    with pytest.raises(ValueError):
        make_envelope("x", status="MAYBE")
    with pytest.raises(TypeError):
        to_jsonable(object())


def test_local_paths_never_leak_into_output(tmp_path):
    assert to_jsonable({"db": tmp_path / "secret_dir" / "research.duckdb"}) == {"db": "research.duckdb"}


def test_a_non_ok_metric_serialises_with_a_null_value():
    result = metric_dict(MetricResult(MetricState.NOT_MEANINGFUL, None, "SIGN_CHANGE"))
    assert result["value"] is None and result["state"] == "NOT_MEANINGFUL" and result["reason"] == "SIGN_CHANGE"


# --- catalog ---


def test_catalog_names_every_screenable_metric_once_with_a_scope_and_unit():
    rows = catalog_json()
    assert len({r["metric"] for r in rows}) == len(rows) == len(CATALOG)
    assert {r["scope"] for r in rows} == {"issuer", "listing"}
    assert CATALOG["revenue_growth_yoy"].unit == "fraction" and CATALOG["fcf"].unit == "usd"


# --- screen specification: strict translation validation ---


def spec(**kw):
    return ScreenSpec.model_validate(kw)


def crit(**kw):
    return Criterion.model_validate(kw)


def test_a_valid_spec_is_normalised_and_percent_thresholds_are_scaled():
    s = spec(
        universe={"symbols": ["aapl", "brk.b", "AAPL"]},
        criteria=[{"metric": "revenue_growth_yoy", "op": "gt", "value": 15, "unit": "percent"}],
    )
    assert s.universe.symbols == ["AAPL", "BRK-B"]
    assert s.criteria[0].scaled() == (pytest.approx(0.15), None, None)


@pytest.mark.parametrize(
    "bad, message",
    [
        ({"metric": "revenue_growth_yoy", "op": "gt", "value": 15}, "fraction metric"),  # ambiguous unit
        ({"metric": "fcf", "op": "gt", "value": 0, "unit": "percent"}, "not a fraction metric"),
        ({"metric": "made_up_metric", "op": "gt", "value": 1}, "unknown metric"),
        ({"metric": "price_to_sales", "op": "lt"}, "needs a value"),
        ({"metric": "price_to_sales", "op": "between", "low": 5, "high": 1}, "above high"),
        ({"metric": "price_to_sales", "op": "between", "value": 5}, "needs low and high"),
        ({"metric": "price_to_sales", "op": "lt", "value": 5, "low": 1}, "needs a value"),
        ({"metric": "price_to_sales", "op": "lt", "value": float("inf")}, "finite"),
        ({"metric": "price_to_sales", "op": "approx", "value": 1}, "Input should be"),
        ({"metric": "price_to_sales", "op": "lt", "value": 5, "surprise": 1}, "Extra inputs"),
    ],
)
def test_invalid_criteria_are_rejected_with_a_precise_message(bad, message):
    with pytest.raises(ValidationError, match=message):
        crit(**bad)


def test_unit_scaling_for_between_and_fraction_units():
    c = crit(metric="drawdown_from_52w_high", op="between", low=-50, high=-30, unit="percent")
    assert c.scaled() == (None, pytest.approx(-0.5), pytest.approx(-0.3))
    assert crit(metric="gross_margin", op="gt", value=0.4, unit="fraction").scaled()[0] == 0.4


def test_a_spec_needs_something_to_filter_and_forbids_unknown_fields():
    with pytest.raises(ValidationError, match="at least one criterion"):
        spec()
    with pytest.raises(ValidationError):
        spec(criteria=[], classification=None, extra_field=1)
    with pytest.raises(ValidationError, match="symbols must not be empty"):
        spec(universe={"symbols": []}, criteria=[{"metric": "fcf", "op": "gt", "value": 0}])
    with pytest.raises(ValidationError):
        spec(universe={"symbols": ["not a ticker!"]}, criteria=[{"metric": "fcf", "op": "gt", "value": 0}])


def test_classification_filters_cannot_mix_taxonomies():
    base = {"criteria": [{"metric": "fcf", "op": "gt", "value": 0}]}
    assert spec(classification={"taxonomy": "nasdaq", "sector": "Technology"}, **base)
    assert spec(classification={"taxonomy": "sec_sic", "sic_codes": ["3674"]}, **base)
    for bad in (
        {"taxonomy": "nasdaq", "sic_codes": ["3674"]},
        {"taxonomy": "sec_sic", "sector": "Technology"},
        {"taxonomy": "sec_sic", "industries": ["Semiconductors"], "sic_codes": ["3674"]},
        {"taxonomy": "nasdaq"},
        {"taxonomy": "sec_sic"},
        {"taxonomy": "gics", "sector": "Information Technology"},
    ):
        with pytest.raises(ValidationError):
            spec(classification=bad, **base)


def test_too_many_criteria_are_rejected():
    many = [{"metric": "fcf", "op": "gt", "value": i} for i in range(21)]
    with pytest.raises(ValidationError):
        spec(criteria=many)


# --- screen evaluation ---


def ok(value, *flags):
    return MetricResult(MetricState.OK, value, None, tuple(flags))


def missing(reason="NOT_REPORTED", state=MetricState.MISSING_INPUT):
    return MetricResult(state, None, reason)


def view(ticker, issuer=None, listing=None, nasdaq=None, sic=None):
    return SecurityView(ticker, "0000000001", f"{ticker} Inc", issuer or {}, listing or {}, nasdaq, sic)


SCREEN = spec(
    criteria=[
        {"metric": "revenue_growth_yoy", "op": "gt", "value": 15, "unit": "percent"},
        {"metric": "fcf", "op": "gt", "value": 0},
        {"metric": "drawdown_from_52w_high", "op": "between", "low": -50, "high": -30, "unit": "percent"},
    ]
)


def test_match_fail_and_missing_data_are_three_distinct_outcomes():
    views = [
        view("PASS", {"revenue_growth_yoy": ok(0.20), "fcf": ok(1.0)}, {"drawdown_from_52w_high": ok(-0.4)}),
        view("FAIL", {"revenue_growth_yoy": ok(0.10), "fcf": ok(1.0)}, {"drawdown_from_52w_high": ok(-0.4)}),
        view(
            "GONE",
            {"revenue_growth_yoy": missing("UNSUPPORTED_TAXONOMY:ifrs-full"), "fcf": ok(1.0)},
            {"drawdown_from_52w_high": ok(-0.4)},
        ),
    ]
    out = run_screen(SCREEN, views)
    assert [r["ticker"] for r in out["matches"]] == ["PASS"]
    assert [r["ticker"] for r in out["failed"]] == ["FAIL"]
    assert [r["ticker"] for r in out["missing_data_exclusions"]] == ["GONE"]
    assert out["counts"] == {"screened": 3, "matches": 1, "failed": 1, "missing_data_exclusions": 1}
    gone = out["missing_data_exclusions"][0]
    assert gone["status"] == UNEVALUABLE and "UNSUPPORTED_TAXONOMY:ifrs-full" in gone["exclusion_reason"]
    assert out["failed"][0]["status"] == NO_MATCH and "revenue_growth_yoy" in out["failed"][0]["exclusion_reason"]


def test_a_missing_metric_is_never_treated_as_zero_or_as_passing():
    """FCF > -1 would pass if a missing FCF were read as 0; it must be UNEVALUABLE instead."""
    s = spec(criteria=[{"metric": "fcf", "op": "gt", "value": -1}])
    for state in MetricState:
        if state is MetricState.OK:
            continue
        result = run_screen(s, [view("X", {"fcf": MetricResult(state, None, "WHY")})])
        assert result["counts"]["matches"] == 0 and result["counts"]["missing_data_exclusions"] == 1, state
    assert run_screen(s, [view("X", {})])["counts"]["missing_data_exclusions"] == 1  # metric not produced at all


def test_every_criterion_reports_value_threshold_state_and_provenance():
    v = SecurityView(
        "AAA",
        "0000000001",
        "AAA",
        {"revenue_growth_yoy": ok(0.2, "RESTATED_PERIOD"), "fcf": ok(5.0)},
        {"drawdown_from_52w_high": ok(-0.35)},
        provenance={"revenue_growth_yoy": [{"accession_no": "0001-26-000001"}]},
    )
    row = run_screen(SCREEN, [v])["matches"][0]
    first = row["criteria"][0]
    assert first["metric"] == "revenue_growth_yoy" and first["value"] == 0.2 and first["result"] == PASS
    assert first["threshold"]["value"] == pytest.approx(0.15) and first["threshold_as_entered"]["unit"] == "percent"
    assert first["flags"] == ["RESTATED_PERIOD"] and first["provenance"] == [{"accession_no": "0001-26-000001"}]
    assert row["criteria_passed"] == 3 and row["status"] == MATCH and row["exclusion_reason"] is None


@pytest.mark.parametrize(
    "op, value, metric_value, expected",
    [
        ("gt", 1, 1, FAIL),
        ("gt", 1, 1.01, PASS),
        ("gte", 1, 1, PASS),
        ("lt", 1, 1, FAIL),
        ("lte", 1, 1, PASS),
        ("eq", 1, 1, PASS),
        ("eq", 1, 1.1, FAIL),
    ],
)
def test_operators_are_exact_about_the_boundary(op, value, metric_value, expected):
    s = spec(criteria=[{"metric": "price_to_sales", "op": op, "value": value}])
    row = run_screen(s, [view("X", {"price_to_sales": ok(metric_value)})])
    outcome = (row["matches"] or row["failed"])[0]["criteria"][0]["result"]
    assert outcome == expected


def test_between_is_inclusive_on_both_ends():
    s = spec(criteria=[{"metric": "price_to_sales", "op": "between", "low": 1, "high": 2}])
    for v, expected in ((1, 1), (2, 1), (0.99, 0), (2.01, 0)):
        assert run_screen(s, [view("X", {"price_to_sales": ok(v)})])["counts"]["matches"] == expected


def test_results_are_alphabetical_with_no_score_or_rank():
    s = spec(criteria=[{"metric": "fcf", "op": "gt", "value": 0}])
    out = run_screen(s, [view(t, {"fcf": ok(i + 1.0)}) for i, t in enumerate(["ZZZ", "AAA", "MMM"])])
    assert [r["ticker"] for r in out["matches"]] == ["AAA", "MMM", "ZZZ"]
    forbidden = {"score", "rank", "ranking", "rating", "best"}
    blob = json.dumps(out).lower()
    assert not any(f'"{k}"' in blob for k in forbidden) and "no score and no ranking" in out["ordering"]


def test_classification_is_evaluated_in_one_taxonomy_and_missing_is_unevaluable():
    s = spec(
        classification={"taxonomy": "nasdaq", "sector": "Technology"},
        criteria=[{"metric": "fcf", "op": "gt", "value": 0}],
    )
    tech = cls.Classification("nasdaq", sector="Technology", industry="Semiconductors")
    energy = cls.Classification("nasdaq", sector="Energy", industry="Oil")
    out = run_screen(
        s,
        [
            view("TECH", {"fcf": ok(1.0)}, nasdaq=tech),
            view("OIL", {"fcf": ok(1.0)}, nasdaq=energy),
            view("NONE", {"fcf": ok(1.0)}),
            view("SICONLY", {"fcf": ok(1.0)}, sic=cls.Classification("sec_sic", industry="x", code="3674")),
        ],
    )
    assert [r["ticker"] for r in out["matches"]] == ["TECH"]
    assert [r["ticker"] for r in out["failed"]] == ["OIL"]
    # a SIC-only security has no Nasdaq classification: unknown, never converted into a Nasdaq sector
    assert [r["ticker"] for r in out["missing_data_exclusions"]] == ["NONE", "SICONLY"]
    sic = spec(
        classification={"taxonomy": "sec_sic", "sic_codes": ["3674"]},
        criteria=[{"metric": "fcf", "op": "gt", "value": 0}],
    )
    out = run_screen(
        sic,
        [
            view("S", {"fcf": ok(1.0)}, sic=cls.Classification("sec_sic", code="3674")),
            view("N", {"fcf": ok(1.0)}, nasdaq=tech),
        ],
    )
    assert [r["ticker"] for r in out["matches"]] == ["S"] and [r["ticker"] for r in out["missing_data_exclusions"]] == [
        "N"
    ]


# --- freshness ---


def test_stale_price_data_is_blocked_not_presented_as_current():
    names = {"price_return_1y", "drawdown_from_52w_high"}
    metrics = {"price_return_1y": ok(0.1), "drawdown_from_52w_high": ok(-0.1), "other": ok(1.0)}
    fresh = fr.apply_listing_freshness(metrics, names, D(2026, 2, 10), D(2026, 2, 13), 7)
    assert fresh == metrics
    stale = fr.apply_listing_freshness(metrics, names, D(2026, 1, 1), D(2026, 2, 13), 7)
    assert stale["price_return_1y"].state is MetricState.MISSING_INPUT and stale["price_return_1y"].reason.startswith(
        "STALE_PRICE_DATA"
    )
    assert stale["other"].ok
    never = fr.apply_listing_freshness(metrics, names, None, D(2026, 2, 13), 7)
    assert never["drawdown_from_52w_high"].reason == "NO_PRICE_DATA"
    assert (
        fr.apply_listing_freshness(metrics, names, D(2026, 1, 1), D(2026, 2, 13), None) == metrics
    )  # explicit opt-out


def test_stale_quotes_block_market_cap_dependent_metrics_only():
    metrics = {"issuer_market_cap": ok(1e9), "price_to_sales": ok(2.0), "revenue_ttm": ok(5e8)}
    out = fr.apply_issuer_freshness(metrics, D(2025, 1, 1), D(2026, 2, 13), 7)
    assert not out["issuer_market_cap"].ok and not out["price_to_sales"].ok and out["revenue_ttm"].ok
    assert fr.freshness_record(D(2026, 2, 1), None, D(2026, 2, 13), 7)["quote_metrics_blocked"] is True


# --- universe ---


def test_symbols_are_canonical_deduplicated_and_bounded():
    assert parse_symbols(["aapl,msft", "BRK.B", "AAPL"]) == ("AAPL", "MSFT", "BRK-B")
    for bad in ([], [""], ["not a ticker!"], [",".join(f"T{i}" for i in range(MAX_INGEST_SYMBOLS + 1))]):
        with pytest.raises(UniverseError):
            parse_symbols(bad, limit=MAX_INGEST_SYMBOLS)


def test_audited_manifest_entries_keep_their_designations_and_others_are_adhoc():
    manifest = {s.symbol: s for s in build_manifest(["GOOG", "GOOGL", "ZZZZ"])}
    assert manifest["GOOGL"].primary_listing and not manifest["GOOG"].primary_listing and manifest["GOOGL"].phase2_cik
    assert (
        manifest["ZZZZ"].roles == ("adhoc",) and manifest["ZZZZ"].phase2_cik is None
    )  # no audited expectation invented


def test_stage_names_always_include_identity():
    assert parse_stages(None) == ("identity", "prices", "quotes", "sec_facts")
    assert parse_stages(["facts"]) == ("identity", "sec_facts")
    with pytest.raises(UniverseError):
        parse_stages(["everything"])


# --- filing-event classification ---


@pytest.mark.parametrize(
    "form, items, sections",
    [
        ("10-K", "", ["filings"]),
        ("10-Q/A", "", ["filings"]),
        ("4", "", ["insiders"]),
        ("144", "", ["insiders"]),
        ("SC 13G/A", "", ["ownership"]),
        ("DEFM14A", "", ["mna"]),
        ("8-K", "2.02,9.01", ["earnings"]),
        ("8-K", "1.01", ["contracts_customers"]),
        ("8-K", "2.01,3.01", ["mna", "legal_regulatory"]),
        ("8-K", "5.02", ["other"]),
        ("8-K", "9.01", ["other"]),
        ("8-K", "", ["other"]),
        ("S-8", "", []),
        ("UPLOAD", "", []),
    ],
)
def test_filings_are_placed_by_form_and_8k_item_only(form, items, sections):
    assert [s for s, _ in sections_for_filing(form, items)] == sections


SUBMISSIONS = {
    "filings": {
        "recent": {
            "accessionNumber": [
                "0000000001-26-000003",
                "0000000001-26-000002",
                "0000000001-25-000009",
                "0000000001-24-000001",
            ],
            "form": ["8-K", "4", "10-K", "8-K"],
            "filingDate": ["2026-02-01", "2026-01-20", "2025-11-01", "2024-01-05"],
            "reportDate": ["2026-01-30", "", "2025-09-27", ""],
            "items": ["2.02,9.01", "", "", "1.01"],
            "primaryDocument": ["a.htm", "b.xml", "c.htm", "d.htm"],
        }
    }
}


def test_classify_submissions_keeps_source_dates_and_provenance_and_reports_the_window():
    company = Company(ticker="AAA", cik="0000000001", name="AAA Inc")
    items, window = classify_submissions(
        SUBMISSIONS,
        company,
        since=D(2025, 1, 1),
        retrieved_at=datetime(2026, 2, 2),
        source_url="https://example.test/sub",
    )
    assert sorted(items) == ["earnings", "filings", "insiders"]  # the 2024 8-K is outside the window
    item = items["earnings"][0]
    assert (
        item.source == "sec"
        and item.source_type == "SEC_FILING"
        and item.filed == D(2026, 2, 1)
        and item.as_of == D(2026, 1, 30)
    )
    assert item.company == company and "2.02 Results of Operations" in item.content
    assert item.provenance["accession_no"] == "0000000001-26-000003"
    assert item.provenance["url"] == "https://www.sec.gov/Archives/edgar/data/1/000000000126000003/a.htm"
    assert item.caveats and "earnings release" in item.caveats[0]
    assert window["since"] == D(2025, 1, 1) and window["oldest_filing_in_document"] == D(2024, 1, 5)


def test_contract_items_carry_the_not_classified_caveat():
    company = Company(ticker="AAA", cik="0000000001")
    items, _ = classify_submissions(
        SUBMISSIONS, company, since=D(2020, 1, 1), retrieved_at=datetime(2026, 2, 2), source_url="u"
    )
    contract = items["contracts_customers"][0]
    assert "does not say whether it is a customer contract" in contract.caveats[0]


def test_a_malformed_submissions_document_yields_nothing_rather_than_guesses():
    company = Company(ticker="AAA", cik="0000000001")
    items, window = classify_submissions(
        {}, company, since=D(2020, 1, 1), retrieved_at=datetime(2026, 2, 2), source_url="u"
    )
    assert items == {} and window["oldest_filing_in_document"] is None
    bad, _ = classify_submissions(
        {"filings": {"recent": {"form": ["8-K"], "filingDate": ["not-a-date"]}}},
        company,
        since=D(2020, 1, 1),
        retrieved_at=datetime(2026, 2, 2),
        source_url="u",
    )
    assert bad == {}


def test_filing_url_uses_the_integer_cik_and_dashless_accession():
    assert (
        filing_url("0000320193", "0000320193-25-000079", "x.htm")
        == "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/x.htm"
    )


def test_the_packet_has_all_sixteen_sections():
    assert len(SECTIONS) == 16 and {"ownership", "insiders", "mna", "legal_regulatory", "government_awards"} <= set(
        SECTIONS
    )


# --- formatting ---


def test_rendering_never_turns_a_missing_metric_into_a_number():
    assert (
        render("fcf", MetricResult(MetricState.MISSING_INPUT, None, "CAPEX_NOT_REPORTED"))
        == "N/A (MISSING_INPUT: CAPEX_NOT_REPORTED)"
    )
    assert render("fcf", None) == "N/A (not produced)"
    assert render("revenue_growth_yoy", ok(0.123)) == "+12.3%"
    assert render("gross_margin", ok(0.45)) == "45.0%"
    assert render("price_to_sales", ok(4.2)) == "4.20x"
    assert money(-1.5e9) == "-$1.50B" and money(12.0) == "$12.00"
