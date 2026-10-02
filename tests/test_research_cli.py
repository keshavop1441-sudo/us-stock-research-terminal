"""The research CLI end to end on SIMULATED providers (hermetic): JSON contracts, deterministic screening, evidence
packets, missing-data and provenance behaviour, source attribution, idempotent ingestion.

Simulated data proves the engine's mechanics, not live provider behaviour (see docs/data_coverage.md)."""

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import duckdb
import p0_fakes as fk
import pytest

from app.cli import research as cli
from app.ingestion.market_source import MarketData
from app.ingestion.sec_http import SecHttpClient
from app.research.catalog import CATALOG

AS_OF = date(2026, 2, 13)
UA = "UnitTestApp contact@example.org"
SYMBOLS = ["AAPL", "NVDA", "TSM", "GOOG", "GOOGL", "BRK-B", "JPM"]
ROOT = Path(__file__).resolve().parent.parent


class Rig:
    def __init__(self, db: Path, monkeypatch, clean_env):
        self.db = db
        self.sec, self.obb = fk.FakeSec(), fk.FakeObb(AS_OF)
        monkeypatch.setattr(
            cli,
            "make_sec",
            lambda ua, stats: (
                SecHttpClient(
                    ua, stats, transport=self.sec.transport(), min_interval=0, sleep=lambda s: None, backoff_base=0
                )
                if ua
                else None
            ),
        )
        monkeypatch.setattr(cli, "make_market", lambda stats: MarketData(lambda: self.obb, stats))
        monkeypatch.chdir(db.parent)

    def call(self, *args, ua=UA, db=True):
        argv = (["--db", str(self.db)] if db else []) + (["--sec-user-agent", ua] if ua else []) + list(args)
        envelope, code = cli.run(argv)
        json.dumps(envelope, allow_nan=False)  # always plain JSON
        return envelope, code

    def ingest(self, *symbols, stages=None):
        extra = ["--stages", *stages] if stages else []
        return self.call(
            "ingest", "--symbols", *symbols, "--as-of", AS_OF.isoformat(), "--price-start", "2024-06-03", *extra
        )


@pytest.fixture
def rig(tmp_path, monkeypatch, clean_env):
    return Rig(tmp_path / "research.duckdb", monkeypatch, clean_env)


@pytest.fixture
def loaded(rig):
    envelope, code = rig.ingest(*SYMBOLS)
    assert code == 0, envelope["errors"]
    return rig


def metrics_of(rig, symbol):
    envelope, _ = rig.call("metrics", "--symbols", symbol, "--as-of", AS_OF.isoformat())
    return envelope["data"]["securities"][0]


# --- envelope contract ---


def test_every_command_answers_with_one_json_envelope(loaded):
    for args in (
        ["catalog"],
        ["universe"],
        ["doctor"],
        ["metrics", "--symbols", "AAPL"],
        ["evidence", "--symbol", "AAPL", "--no-events"],
    ):
        envelope, code = loaded.call(*args)
        assert set(envelope) == {"schema_version", "command", "status", "generated_at", "data", "warnings", "errors"}
        assert envelope["command"] == args[0] and envelope["status"] in ("OK", "PARTIAL", "ERROR") and code in (0, 1)


def test_invalid_arguments_produce_an_error_envelope_and_exit_code_2(rig):
    for args in (["bogus"], ["ingest"], ["metrics", "--symbols", "AAPL", "--as-of", "yesterday"], []):
        envelope, code = rig.call(*args)
        assert code == 2 and envelope["status"] == "ERROR" and envelope["errors"][0]["code"] == "INVALID_ARGUMENTS"


def test_main_prints_exactly_one_json_line(rig, capsys, monkeypatch):
    assert cli.main(["--db", str(rig.db), "catalog"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and json.loads(out)["command"] == "catalog"
    assert cli.main(["--db", str(rig.db), "--pretty", "catalog"]) == 0
    assert capsys.readouterr().out.count("\n") > 10


def test_catalog_lists_metrics_and_the_screen_schema(rig):
    data = rig.call("catalog")[0]["data"]
    assert {m["metric"] for m in data["metrics"]} == set(CATALOG)
    assert "criteria" in data["screen_schema"]["properties"] and data["operators"] == [
        "gt",
        "gte",
        "lt",
        "lte",
        "eq",
        "between",
    ]


# --- ingestion ---


def test_ingest_stores_data_and_reports_each_stage_per_symbol(loaded, rig):
    universe = rig.call("universe", "--as-of", AS_OF.isoformat())[0]["data"]
    assert {s["ticker"] for s in universe["securities"]} == set(SYMBOLS)
    assert "full market universe is not loaded" in universe["scope_note"]
    tsm = next(s for s in universe["securities"] if s["ticker"] == "TSM")
    assert tsm["fundamentals_unavailable_reason"] == "UNSUPPORTED_TAXONOMY:ifrs-full" and tsm["fact_rows"] == 0
    assert tsm["last_price_bar"] == AS_OF.isoformat()  # identity/prices still available


def test_ingest_reports_ifrs_as_expected_unsupported_not_failure(rig):
    envelope, code = rig.ingest("TSM")
    assert code == 0 and envelope["status"] == "PARTIAL"
    data = envelope["data"]
    assert data["expected_unsupported"][0]["taxonomy"] == "ifrs-full" and data["issues"] == []
    assert [w["code"] for w in envelope["warnings"]] == ["EXPECTED_UNSUPPORTED"]


def test_ingesting_twice_is_idempotent(rig):
    first = rig.ingest("AAPL", "NVDA")[0]["data"]["tables"]
    second = rig.ingest("AAPL", "NVDA")[0]["data"]["tables"]
    assert first["financial_facts"]["inserted"] > 0
    assert all(c["inserted"] == 0 and c["updated"] == 0 for c in second.values())
    assert second["financial_facts"]["unchanged"] == first["financial_facts"]["inserted"]


def test_ingest_without_a_sec_user_agent_makes_no_sec_request_and_never_invents_one(rig):
    envelope, code = rig.call("ingest", "--symbols", "AAPL", ua=None)
    assert code == 1 and envelope["errors"][0]["code"] == "SEC_USER_AGENT_MISSING"
    assert rig.sec.requests == [] and rig.obb.calls == []


def test_an_invalid_sec_user_agent_is_reported_and_not_used(rig):
    envelope, code = rig.call("resolve", "--symbols", "AAPL", ua="justaname")
    assert code == 1 and envelope["errors"][0]["code"] == "SEC_USER_AGENT_MISSING"
    assert "SEC_USER_AGENT" in envelope["errors"][0]["message"]
    assert rig.sec.requests == []


def test_the_user_agent_sent_to_sec_is_the_callers_own(rig):
    rig.call("resolve", "--symbols", "AAPL")
    assert rig.sec.user_agents == {UA}


def test_too_many_symbols_per_call_are_refused(rig):
    envelope, code = rig.call("ingest", "--symbols", *[f"T{i}" for i in range(26)])
    assert code == 2 and envelope["errors"][0]["code"] == "INVALID_SYMBOLS"


def test_sec_403_is_reported_not_retried(rig):
    rig.sec.forced["company_tickers_exchange"] = [403]
    envelope, code = rig.ingest("AAPL")
    assert envelope["status"] in ("PARTIAL", "ERROR") and any(w["code"] == "HTTP_403" for w in envelope["warnings"])
    assert len([u for u in rig.sec.requests if "company_tickers" in u]) == 1


def test_an_unknown_symbol_fails_identity_and_nothing_is_guessed(rig):
    envelope, _ = rig.ingest("AAPL", "ZZZZ")
    assert any(w["code"] == "IDENTITY" and "ZZZZ" in w["message"] for w in envelope["warnings"])
    assert {s["ticker"] for s in rig.call("universe")[0]["data"]["securities"]} == {"AAPL"}


def test_resolve_reports_resolved_and_unresolved_with_provenance(rig):
    envelope, code = rig.call("resolve", "--symbols", "AAPL", "ZZZZ")
    data = envelope["data"]
    assert code == 0 and envelope["status"] == "PARTIAL"
    assert data["resolved"]["AAPL"]["cik"] == "0000320193" and "ZZZZ" in data["unresolved"]
    assert data["provenance"]["provider"] == "sec" and data["provenance"]["content_hash"].startswith("sha256:")


# --- metrics: provenance and missing-data semantics ---


def test_every_catalogued_metric_is_produced_for_an_ordinary_issuer(loaded):
    sec = metrics_of(loaded, "AAPL")
    produced = set(sec["issuer_metrics"]) | set(sec["listing_metrics"])
    assert set(CATALOG) <= produced, sorted(set(CATALOG) - produced)
    long_history = {"price_return_3y", "price_return_5y"}  # the simulated window is ~20 months
    for name, spec in CATALOG.items():
        group = sec["issuer_metrics"] if spec.scope == "issuer" else sec["listing_metrics"]
        if name in long_history:
            assert group[name]["reason"] == "INSUFFICIENT_HISTORY" and group[name]["value"] is None
        else:
            assert group[name]["state"] == "OK", (name, group[name])


def test_metrics_carry_filing_and_retrieval_provenance(loaded):
    sec = metrics_of(loaded, "AAPL")
    revenue = sec["lines"]["revenue"]
    assert (
        revenue["accession"]
        and revenue["form"] == "10-K"
        and revenue["tag"]
        and revenue["filed"]
        and revenue["period_end"]
    )
    for kind in ("price", "quote", "facts"):
        source = sec["retrievals"][kind]
        assert (
            source["provider"]
            and source["command"]
            and source["retrieved_at"]
            and source["content_hash"].startswith("sha256:")
        )
    assert sec["retrievals"]["price"]["provider"] == "cboe" and sec["retrievals"]["quote"]["provider"] == "nasdaq"


def test_unsupported_taxonomy_leaves_every_fundamental_missing_never_zero(loaded):
    tsm = metrics_of(loaded, "TSM")
    assert tsm["coverage"]["fundamentals"].startswith("UNAVAILABLE:UNSUPPORTED_TAXONOMY")
    for name in (
        "revenue_growth_yoy",
        "fcf",
        "gross_margin",
        "total_debt",
        "debt_to_equity_change_yoy",
        "price_to_sales",
    ):
        metric = tsm["issuer_metrics"][name]
        assert (
            metric["state"] == "MISSING_INPUT"
            and metric["value"] is None
            and "UNSUPPORTED_TAXONOMY" in metric["reason"]
        ), name
    assert tsm["listing_metrics"]["price_return_1y"]["state"] == "OK"  # prices still work
    assert tsm["issuer_metrics"]["issuer_market_cap"]["state"] == "OK"


def test_absent_debt_lines_stay_missing_and_never_become_zero(loaded):
    brk = metrics_of(loaded, "BRK-B")
    assert brk["issuer_metrics"]["total_debt"]["state"] == "MISSING_INPUT"
    assert brk["issuer_metrics"]["debt_to_equity_change_yoy"]["state"] == "MISSING_INPUT"
    assert brk["issuer_metrics"]["eps_growth_yoy"]["value"] is None  # no EPS reported: unknown, not 0


def test_multi_class_listings_share_the_issuer_level_market_cap(loaded):
    goog, googl = metrics_of(loaded, "GOOG"), metrics_of(loaded, "GOOGL")
    assert goog["issuer_metrics"]["issuer_market_cap"]["value"] == googl["issuer_metrics"]["issuer_market_cap"]["value"]
    assert goog["cross_checks"]["primary_listing"] == googl["cross_checks"]["primary_listing"] == "GOOGL"
    assert goog["cross_checks"]["primary_listing_designation"] == "P0_MANIFEST"
    assert "MULTI_CLASS_PER_SHARE_BASIS_UNVERIFIED" in goog["listing_metrics"]["price_to_earnings"]["flags"]


def test_metrics_for_a_symbol_that_is_not_stored_says_so(loaded):
    envelope, _ = loaded.call("metrics", "--symbols", "AAPL", "MSFT")
    assert envelope["status"] == "PARTIAL" and envelope["data"]["not_in_database"] == ["MSFT"]
    assert any(w["code"] == "NOT_IN_DATABASE" for w in envelope["warnings"])


def test_reading_without_a_database_asks_for_ingest_first(rig):
    for args in (["universe"], ["metrics", "--symbols", "AAPL"], ["evidence", "--symbol", "AAPL"]):
        envelope, code = rig.call(*args)
        assert code == 2 and envelope["errors"][0]["code"] == "NO_DATABASE"


def test_stale_price_data_is_blocked_unless_explicitly_allowed(loaded):
    later = "2026-06-01"
    blocked = loaded.call("metrics", "--symbols", "AAPL", "--as-of", later)[0]["data"]["securities"][0]
    assert blocked["listing_metrics"]["price_return_1y"]["reason"].startswith("STALE_PRICE_DATA")
    assert blocked["issuer_metrics"]["price_to_sales"]["reason"].startswith("STALE_QUOTE")
    assert blocked["issuer_metrics"]["revenue_growth_yoy"]["state"] == "OK"  # filings are dated by themselves
    assert blocked["freshness"]["price_age_days"] == (date.fromisoformat(later) - AS_OF).days
    allowed = loaded.call("metrics", "--symbols", "AAPL", "--as-of", later, "--allow-stale")[0]["data"]["securities"][0]
    assert allowed["listing_metrics"]["price_return_1y"]["state"] == "OK"


# --- screening ---

SPEC = {
    "name": "t",
    "criteria": [
        {"metric": "revenue_growth_yoy", "op": "gt", "value": 5, "unit": "percent"},
        {"metric": "fcf", "op": "gt", "value": 0},
        {"metric": "gross_margin_change_yoy", "op": "gte", "value": 0, "unit": "fraction"},
    ],
}


def screen(rig, spec=SPEC, *extra):
    return rig.call("screen", "--spec-json", json.dumps(spec), "--as-of", AS_OF.isoformat(), *extra)


def test_screen_separates_matches_failures_and_missing_data_exclusions(loaded):
    envelope, code = screen(loaded)
    data = envelope["data"]
    names = {k: [r["ticker"] for r in data[k]] for k in ("matches", "failed", "missing_data_exclusions")}
    assert {"AAPL", "NVDA"} <= set(names["matches"]) and "TSM" in names["missing_data_exclusions"]
    assert {"BRK-B", "JPM"} & set(
        names["missing_data_exclusions"] + names["failed"]
    )  # sparse reporters never match on zeros
    assert (
        envelope["status"] == "PARTIAL"
        and code == 0
        and any(w["code"] == "MISSING_DATA_EXCLUSIONS" for w in envelope["warnings"])
    )
    assert data["universe"]["full_market_universe_loaded"] is False and data["counts"]["screened"] == len(SYMBOLS)
    assert sorted(names["matches"]) == names["matches"]  # alphabetical, no ranking


def test_screen_rows_carry_exact_values_thresholds_states_and_provenance(loaded):
    data = screen(loaded)[0]["data"]
    row = next(r for r in data["matches"] if r["ticker"] == "AAPL")
    growth = row["criteria"][0]
    assert growth["metric"] == "revenue_growth_yoy" and growth["result"] == "PASS" and growth["metric_state"] == "OK"
    assert growth["value"] == pytest.approx(metrics_of(loaded, "AAPL")["issuer_metrics"]["revenue_growth_yoy"]["value"])
    assert growth["threshold"]["value"] == pytest.approx(0.05) and growth["threshold_as_entered"] == {
        "value": 5,
        "low": None,
        "high": None,
        "unit": "percent",
    }
    assert growth["provenance"][0]["accession"] and growth["provenance"][0]["form"] == "10-K"
    tsm = next(r for r in data["missing_data_exclusions"] if r["ticker"] == "TSM")
    assert tsm["exclusion_reason"].startswith("MISSING_DATA") and all(c["value"] is None for c in tsm["criteria"])
    assert "UNSUPPORTED_TAXONOMY" in tsm["exclusion_reason"]


def test_screen_filters_deterministically_and_repeatably(loaded):
    first, second = screen(loaded)[0]["data"], screen(loaded)[0]["data"]
    assert first == second
    impossible = screen(
        loaded, {"criteria": [{"metric": "revenue_growth_yoy", "op": "gt", "value": 500, "unit": "percent"}]}
    )[0]["data"]
    assert impossible["matches"] == [] and len(impossible["failed"]) + len(
        impossible["missing_data_exclusions"]
    ) == len(SYMBOLS)


def test_screen_by_taxonomy_uses_the_named_system_only(loaded):
    nasdaq = screen(
        loaded,
        {
            "classification": {"taxonomy": "nasdaq", "sector": "Technology"},
            "criteria": [{"metric": "fcf", "op": "gt", "value": 0}],
        },
    )[0]["data"]
    assert "AAPL" in [r["ticker"] for r in nasdaq["matches"]]
    sic = screen(
        loaded,
        {
            "classification": {"taxonomy": "sec_sic", "sic_codes": ["3571"]},
            "criteria": [{"metric": "fcf", "op": "gt", "value": 0}],
        },
    )[0]["data"]
    assert "AAPL" in [r["ticker"] for r in sic["matches"]]  # synthetic SIC 3571 for AAPL
    none = screen(
        loaded,
        {
            "classification": {"taxonomy": "sec_sic", "sic_codes": ["9999"]},
            "criteria": [{"metric": "fcf", "op": "gt", "value": 0}],
        },
    )[0]["data"]
    assert none["matches"] == [] and none["counts"]["failed"] > 0


def test_screen_can_be_restricted_to_named_symbols_and_reports_unstored_ones(loaded):
    spec = {"universe": {"symbols": ["AAPL", "MSFT"]}, "criteria": [{"metric": "fcf", "op": "gt", "value": 0}]}
    envelope, _ = screen(loaded, spec)
    assert [r["ticker"] for r in envelope["data"]["matches"]] == ["AAPL"]
    assert envelope["data"]["universe"]["requested_but_not_in_database"] == ["MSFT"]
    assert any(w["code"] == "NOT_IN_DATABASE" for w in envelope["warnings"])


def test_screen_with_ingest_missing_ingests_then_screens(rig):
    spec = {"universe": {"symbols": ["AAPL"]}, "criteria": [{"metric": "fcf", "op": "gt", "value": 0}]}
    envelope, code = rig.call(
        "screen", "--spec-json", json.dumps(spec), "--as-of", AS_OF.isoformat(), "--ingest-missing"
    )
    assert code == 0 and [r["ticker"] for r in envelope["data"]["matches"]] == ["AAPL"]


def test_an_empty_database_gives_an_error_not_an_empty_match_list(rig):
    from app.database.connection import ensure_database

    ensure_database(rig.db)
    envelope, code = screen(rig)
    assert code == 1 and envelope["errors"][0]["code"] == "EMPTY_UNIVERSE"


def test_a_bad_screen_spec_is_rejected_before_anything_runs(loaded):
    bad = {"criteria": [{"metric": "revenue_growth_yoy", "op": "gt", "value": 15}]}  # ambiguous unit
    envelope, code = loaded.call("screen", "--spec-json", json.dumps(bad))
    assert code == 2 and envelope["errors"][0]["code"] == "INVALID_SCREEN_SPEC"
    assert "fraction metric" in json.dumps(envelope["errors"][0]["details"])
    envelope, code = loaded.call("screen", "--spec-json", "{not json")
    assert code == 2 and envelope["errors"][0]["code"] == "INVALID_SCREEN_SPEC"
    envelope, code = loaded.call("screen", "--spec", "/nonexistent/spec.json")
    assert (
        code == 2 and envelope["errors"][0]["code"] == "SPEC_UNREADABLE" and "nonexistent" not in json.dumps(envelope)
    )


def test_validate_screen_normalises_and_runs_nothing(rig):
    envelope, code = rig.call("validate-screen", "--spec-json", json.dumps(SPEC))
    assert code == 0 and envelope["data"]["valid"] is True
    assert envelope["data"]["thresholds_in_stored_units"][0]["value"] == pytest.approx(0.05)
    assert not rig.db.exists()  # validation touches no database


def test_the_screen_spec_can_be_read_from_a_file(loaded, tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(SPEC))
    envelope, code = loaded.call("screen", "--spec", str(path), "--as-of", AS_OF.isoformat())
    assert code == 0 and envelope["data"]["counts"]["screened"] == len(SYMBOLS)


# --- evidence packets ---


def with_filings(rig):
    rig.sec.forms["AAPL"] = (
        ("8-K", "2026-02-01", "2026-01-30", "2.02,9.01"),
        ("8-K", "2026-01-15", "", "1.01"),
        ("4", "2026-01-20", ""),
        ("10-Q", "2026-01-30", "2025-12-27"),
        ("SC 13G", "2026-01-10", ""),
        ("8-K", "2025-11-01", "", "2.01,3.01"),
    )


def packet(rig, symbol="AAPL", *extra):
    envelope, code = rig.call("evidence", "--symbol", symbol, "--as-of", AS_OF.isoformat(), *extra)
    return envelope, code


def test_the_packet_has_every_section_with_an_explicit_status(loaded):
    with_filings(loaded)
    envelope, code = packet(loaded)
    sections = envelope["data"]["sections"]
    assert code == 0 and list(sections) == [
        "identity",
        "price",
        "fundamentals",
        "valuation",
        "balance_sheet",
        "ownership",
        "insiders",
        "filings",
        "earnings",
        "news_events",
        "contracts_customers",
        "partnerships",
        "mna",
        "legal_regulatory",
        "government_awards",
        "other",
    ]
    assert all(
        s["status"] in ("OK", "NONE_FOUND", "NOT_COLLECTED", "UNAVAILABLE", "UNSUPPORTED") for s in sections.values()
    )
    for name in ("news_events", "partnerships", "government_awards"):
        assert (
            sections[name]["status"] == "NOT_COLLECTED"
            and "publisher, URL and publication date" in sections[name]["guidance"]
        )
    assert sections["insiders"]["status"] == "OK" and "NOT parsed" in sections["insiders"]["guidance"]


def test_every_evidence_item_keeps_source_date_company_content_and_provenance(loaded):
    with_filings(loaded)
    sections = packet(loaded)[0]["data"]["sections"]
    count = 0
    for section in sections.values():
        for item in section["items"]:
            count += 1
            assert (
                item["source"]
                and item["source_type"]
                and item["content"]
                and item["id"].startswith(item["section"] + ":")
            )
            assert item["company"]["ticker"] == "AAPL" and item["company"]["cik"] == "0000320193"
            assert item["as_of"] or item["filed"]
            assert item["provenance"]
    assert count >= 10
    event = sections["earnings"]["items"][0]
    assert (
        event["source_type"] == "SEC_FILING"
        and event["provenance"]["accession_no"]
        and event["provenance"]["url"].startswith("https://www.sec.gov/Archives/edgar/data/320193/")
    )
    assert event["provenance"]["submissions_content_hash"].startswith("sha256:")


def test_filings_are_classified_into_sections_without_interpretation(loaded):
    with_filings(loaded)
    sections = packet(loaded)[0]["data"]["sections"]
    assert [i["data"]["form"] for i in sections["insiders"]["items"]] == ["4"]
    assert [i["data"]["form"] for i in sections["ownership"]["items"]] == ["SC 13G"]
    assert [i["data"]["items"] for i in sections["contracts_customers"]["items"]] == [["1.01"]]
    assert [i["data"]["items"] for i in sections["mna"]["items"]] == [["2.01"]]
    assert [i["data"]["items"] for i in sections["legal_regulatory"]["items"]] == [["3.01"]]
    assert "customer contract" in sections["contracts_customers"]["items"][0]["caveats"][0]
    assert sections["filings"]["items"][0]["data"]["form"] == "10-Q"


def test_numeric_sections_expose_metric_states_and_list_what_is_missing(loaded):
    sections = packet(loaded, "BRK-B", "--no-events")[0]["data"]["sections"]
    assert sections["balance_sheet"]["status"] == "OK"
    missing = {m["metric"]: m for m in sections["balance_sheet"]["missing"]}
    assert (
        missing["total_debt"]["state"] == "MISSING_INPUT" and "DEBT_COMPONENT_ABSENT" in missing["total_debt"]["reason"]
    )
    assert any(m["metric"] == "eps_growth_yoy" for m in sections["fundamentals"]["missing"])
    metrics = sections["balance_sheet"]["items"][0]["data"]["metrics"]
    assert metrics["total_debt"]["value"] is None  # never 0


def test_an_unsupported_taxonomy_issuer_gets_unsupported_fundamentals_but_real_prices(loaded):
    envelope, _ = packet(loaded, "TSM", "--no-events")
    sections = envelope["data"]["sections"]
    for name in ("fundamentals", "balance_sheet"):
        assert sections[name]["status"] == "UNSUPPORTED" and sections[name]["items"] == []
        assert (
            "UNSUPPORTED_TAXONOMY" in sections[name]["missing"][0]["reason"]
            and "no US-GAAP facts are invented" in sections[name]["guidance"]
        )
    assert sections["price"]["status"] == "OK" and sections["identity"]["status"] == "OK"
    assert (
        envelope["data"]["sections"]["valuation"]["items"][0]["data"]["metrics"]["price_to_sales"]["state"]
        == "MISSING_INPUT"
    )


def test_the_packet_without_events_marks_filing_sections_not_collected_with_the_reason(loaded):
    sections = packet(loaded, "AAPL", "--no-events")[0]["data"]["sections"]
    assert (
        sections["insiders"]["status"] == "NOT_COLLECTED"
        and sections["insiders"]["missing"][0]["reason"] == "EVENTS_NOT_REQUESTED"
    )
    assert (
        sections["earnings"]["items"][0]["source_type"] == "SEC_XBRL_FACTS"
    )  # annual figures still come from stored facts


def test_without_a_sec_user_agent_event_sections_say_why(loaded):
    envelope, _ = loaded.call("evidence", "--symbol", "AAPL", "--as-of", AS_OF.isoformat(), ua=None)
    assert envelope["data"]["sections"]["insiders"]["missing"][0]["reason"] == "SEC_USER_AGENT_MISSING"


def test_when_sec_blocks_the_request_the_section_reports_the_failure(loaded):
    loaded.sec.forced["submissions/CIK0000320193"] = [403]
    sections = packet(loaded)[0]["data"]["sections"]
    assert sections["filings"]["status"] == "NOT_COLLECTED" and sections["filings"]["missing"][0]["reason"].startswith(
        "HTTP_403"
    )


def test_packet_states_what_it_does_not_do(loaded):
    data = packet(loaded, "AAPL", "--no-events")[0]["data"]
    assert any("causality" in limit for limit in data["limits"]) and any(
        "PRICE returns" in limit for limit in data["limits"]
    )
    assert data["freshness"]["price_metrics_blocked"] is False


def test_evidence_for_an_unstored_symbol_asks_for_ingest(loaded):
    envelope, code = packet(loaded, "MSFT")
    assert code == 1 and envelope["errors"][0]["code"] == "NOT_IN_DATABASE"


def test_events_command_is_live_read_only_and_writes_nothing(rig):
    with_filings(rig)
    envelope, code = rig.call("events", "--symbols", "AAPL", "--since", "2025-06-01")
    company = envelope["data"]["companies"]["AAPL"]
    assert code == 0 and company["status"] == "OK" and not rig.db.exists()
    assert {"earnings", "insiders", "ownership", "mna"} <= set(company["sections"])
    assert company["window"]["since"] == "2025-06-01"


# --- doctor ---


def test_doctor_reports_readiness_without_leaking_the_contact(loaded):
    envelope, code = loaded.call("doctor")
    blob = json.dumps(envelope)
    assert code == 0 and envelope["data"]["capabilities"]["read_stored_data_and_screen"] is True
    assert "contact@example.org" not in blob and "UnitTestApp" not in blob
    checks = {c["name"]: c for c in envelope["data"]["checks"]}
    assert checks["sec_user_agent"]["status"] == "OK" and checks["database"]["status"] == "OK"
    assert str(loaded.db.parent) not in blob  # no local paths
    assert "schema v3" in checks["database"]["detail"]
    missing_ua, _ = loaded.call("doctor", ua=None)
    assert {c["name"]: c for c in missing_ua["data"]["checks"]}["sec_user_agent"]["status"] == "WARN"
    assert missing_ua["data"]["capabilities"]["live_sec_data_and_filing_events"] is False


# --- the launcher runs as a real process ---


def test_the_skill_launcher_runs_as_a_subprocess_and_prints_json(tmp_path):
    launcher = ROOT / "claude" / "skills" / "us-stock-research" / "scripts" / "research.py"
    done = subprocess.run(
        [sys.executable, str(launcher), "--db", str(tmp_path / "x.duckdb"), "catalog"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["command"] == "catalog" and done.stdout.count("\n") == 1


def test_the_database_written_by_the_cli_has_the_current_schema(loaded):
    with duckdb.connect(str(loaded.db)) as con:
        assert con.execute("SELECT count(*) FROM securities").fetchone()[0] == len(SYMBOLS)


def test_doctor_is_read_only_and_does_not_create_the_database(rig):
    envelope, code = rig.call("doctor")
    assert code == 0 and not rig.db.exists()
    assert {c["name"]: c for c in envelope["data"]["checks"]}["database"]["detail"].startswith("no database yet")


def test_doctor_network_probe_reports_each_host_and_the_sec_access_check(rig, monkeypatch):
    import httpx

    from app.services import research_doctor_service as svc

    handler = lambda request: httpx.Response(403 if "cboe" in str(request.url) else 200)  # noqa: E731
    checks = svc.probe_hosts(transport=httpx.MockTransport(handler))
    assert {c["name"] for c in checks} == {
        "network:sec_www",
        "network:sec_data",
        "network:nasdaq_api",
        "network:cboe_cdn",
    }
    assert all(c["status"] == "OK" for c in checks)  # any HTTP answer proves reachability

    def refuse(request):
        raise httpx.ConnectError("blocked")

    blocked = svc.probe_hosts(transport=httpx.MockTransport(refuse))
    assert all(c["status"] == "FAIL" and "ConnectError" in c["detail"] for c in blocked)
    monkeypatch.setattr(
        svc, "probe_hosts", lambda **kw: [{"name": "network:stub", "status": "OK", "detail": "stub"}]
    )  # hermetic
    envelope, _ = rig.call("doctor", "--network")
    assert any(c["name"] == "sec_access" and c["status"] == "OK" for c in envelope["data"]["checks"])


def test_reads_during_a_refresh_are_unavailable_never_stale_data(loaded, hold):
    with hold("writer"):
        for args in (["universe"], ["metrics", "--symbols", "AAPL"], ["evidence", "--symbol", "AAPL", "--no-events"]):
            envelope, code = loaded.call(*args)
            assert code == 1 and envelope["status"] == "ERROR" and envelope["data"] == {}
            assert envelope["errors"][0]["code"] == "DATABASE_UNAVAILABLE"
    assert loaded.call("universe")[1] == 0  # and it works again once the writer is gone
