"""P0 end to end on SIMULATED providers (hermetic): ingestion, idempotency, provenance, failure handling, derived
metrics.

What this proves is the pipeline's mechanics. It is NOT evidence that SEC, Nasdaq or Cboe behave this way: live
behaviour is only established by a live run (see docs/data_coverage.md, P0 section).
"""

from datetime import date

import duckdb
import httpx
import p0_fakes as fk
import p0_synthetic as syn
import pytest

from app.database import access
from app.ingestion.market_source import MarketData
from app.ingestion.sec_http import SecHttpClient
from app.ingestion.stats import RequestStats
from app.screening.metrics import MetricState
from app.services import p0_metrics_service as ms
from app.services.ingestion_service import P0Run, run_p0_sequence

AS_OF = date(2026, 2, 13)
UA = "UnitTestApp contact@example.org"


class Rig:
    def __init__(self, db_path, tmp_path):
        self.db_path, self.tmp_path = db_path, tmp_path
        self.sec, self.obb = fk.FakeSec(), fk.FakeObb(AS_OF)
        self.user_agent: str | None = UA
        self.runs = 0

    def make(self, run_id=None):
        self.runs += 1
        stats = RequestStats()
        sec = (
            SecHttpClient(
                self.user_agent,
                stats,
                transport=self.sec.transport(),
                min_interval=0,
                sleep=lambda s: None,
                backoff_base=0,
            )
            if self.user_agent
            else None
        )
        return P0Run(
            self.db_path, run_id=run_id or f"t{self.runs}", mode="SIMULATED", as_of=AS_OF, sec=sec,
            market=MarketData(lambda: self.obb, stats), stats=stats, raw_dir=self.tmp_path / "raw",
            price_start=date(2024, 6, 3),  # a short window keeps the suite fast
        )  # fmt: skip

    def run(self):
        return self.make().run()


@pytest.fixture
def rig(db_path, tmp_path):
    return Rig(db_path, tmp_path)


@pytest.fixture(scope="module")
def completed(tmp_path_factory):
    """One full two-run sequence, shared by the read-only assertions below."""
    from app.database.connection import ensure_database

    root = tmp_path_factory.mktemp("p0")
    db = root / "p0.duckdb"
    ensure_database(db)
    rig = Rig(db, root)
    report = run_p0_sequence(db, lambda run_id: rig.make(run_id))
    return rig, report


def count(db_path, table):
    con = duckdb.connect(str(db_path))
    try:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


# --- the happy path ------------------------------------------------------------------------------------------------


def test_every_stage_runs_and_the_ifrs_issuer_is_an_expected_gap_not_a_failure(completed):
    _, report = completed
    run = report["first_run"]
    assert run["fatal"] is None
    assert {n: s["status"] for n, s in run["stages"].items()} == dict.fromkeys(
        ("identity", "prices", "quotes", "sec_facts"), "OK"
    )
    assert run["issues"] == []  # no genuine failure
    facts = run["stages"]["sec_facts"]
    assert (facts["attempted"], facts["succeeded"], facts["failed"], facts["expected_unsupported"]) == (13, 12, 0, 1)
    assert run["stages"]["identity"]["succeeded"] == 14
    assert [(e["symbol"], e["kind"], e["taxonomy"]) for e in run["expected_unsupported"]] == [
        ("TSM", "UNSUPPORTED_TAXONOMY", "ifrs-full")
    ]
    assert run["outcomes"]["TSM"]["facts_status"] == "UNSUPPORTED_TAXONOMY" and not run["outcomes"]["TSM"]["facts"]


def test_multi_class_listings_share_one_issuer_and_one_market_cap(completed):
    rig, report = completed
    assert report["first_run"]["multi_class"] == {"0001652044": ["GOOGL", "GOOG"]}
    metrics = report["metrics"]["0001652044"]
    assert metrics["issuer_metrics"]["issuer_market_cap"]["state"] == "OK"
    assert "MULTI_CLASS_ALL_SHARES_AT_PRIMARY_PRICE" in metrics["issuer_metrics"]["issuer_market_cap"]["flags"]
    assert set(metrics["listing_metrics"]) == {
        "GOOGL",
        "GOOG",
    }  # per-listing price metrics, ONE issuer-level cap and P/S
    assert metrics["cross_checks"]["primary_listing"] == "GOOGL"
    with access.reader(rig.db_path) as r:
        listings = r.find_securities_by_cik("0001652044")
        assert listings["ticker"].to_list() == ["GOOG", "GOOGL"]
        assert r.find_securities_by_ticker("GOOGL")["cik"].to_list() == ["0001652044"]
    assert metrics["listing_metrics"]["GOOG"]["price_to_earnings"]["flags"] == [
        "MULTI_CLASS_PER_SHARE_BASIS_UNVERIFIED"
    ]


def test_class_share_ticker_spelling_is_translated_per_provider_but_stored_in_sec_spelling(completed):
    rig, _ = completed
    spelled = {symbol for _, _, symbol in rig.obb.calls}
    assert "BRK.B" in spelled and "BRK-B" not in spelled  # Nasdaq/Cboe spelling on the wire (Cboe rejects BRK-B)
    with access.reader(rig.db_path) as r:
        assert r.find_securities_by_ticker("BRK-B")["cik"].to_list() == ["0001067983"]
        assert r.find_securities_by_ticker("BRK.B").is_empty()  # the provider spelling is never stored


def test_second_identical_run_inserts_and_changes_nothing(completed):
    _, report = completed
    idem = report["idempotency"]
    assert idem["run1"]["inserted"] > 5000 and idem["run1"]["unchanged"] == 0
    assert (idem["run2"]["inserted"], idem["run2"]["updated"]) == (0, 0)
    assert idem["run2"]["unchanged"] == idem["run1"]["inserted"]
    after1, after2 = idem["table_counts_after_run1"], idem["table_counts_after_run2"]
    assert {t: n for t, n in after1.items() if t != "sources"} == {t: n for t, n in after2.items() if t != "sources"}
    assert after2["sources"] == 2 * after1["sources"]  # the append-only retrieval log grows by one row per retrieval
    assert idem["verdict"].startswith("PASS")
    assert all(c["inserted"] == 0 and c["updated"] == 0 for c in idem["per_table_run2"].values())


def test_a_rerun_appends_retrieval_rows_but_never_data_rows(completed):
    _, report = completed
    assert report["idempotency"]["retrieval_rows_appended_run2"] > 0  # each retrieval is its own provenance row


def test_validation_finds_no_violation_after_either_run(completed):
    _, report = completed
    assert all(v == 0 for v in report["validation_after_run1"]["integrity"].values()), report["validation_after_run1"][
        "integrity"
    ]
    acceptance = {c["id"]: c["status"] for c in report["acceptance"]}
    assert acceptance["A6"] == acceptance["A13"] == "PASS"  # every required component is mechanical and was measured
    assert acceptance["A11"] == "PARTIAL"  # the re-run component passed; three components are test-verified only
    assert all(acceptance[k] != "PASS" for k in ("A1", "A3", "A4", "A5", "A7", "A8", "A9", "A10", "A12"))  # simulated


def test_provenance_of_every_retrieval(completed):
    rig, _ = completed
    con = duckdb.connect(str(rig.db_path))
    try:
        rows = con.execute(
            "SELECT provider, dataset, command, parameters, provider_version, content_hash, retrieved_at, "
            "is_fallback, url "
            "FROM sources"
        ).fetchall()
        assert {r[0] for r in rows} == {"sec", "cboe", "nasdaq"}
        assert all(r[2] and r[3] and r[5].startswith("sha256:") and r[6] for r in rows)
        sec = [r for r in rows if r[0] == "sec"]
        assert all(r[2].startswith("HTTP GET https://") and r[8] for r in sec)
        openbb = [r for r in rows if r[0] != "sec"]
        assert all(r[2].startswith(f"obb.{r[0]}.equity.") and r[4].startswith(f"openbb-{r[0]} ") for r in openbb)
        assert not any(r[7] for r in rows)  # no fallback was needed
        assert con.execute(
            "SELECT count(*) FROM financial_facts WHERE source_id IS NULL OR accession_no IS NULL"
        ).fetchone() == (0,)
        assert con.execute("SELECT count(*) FROM price_daily WHERE adj_close IS NOT NULL").fetchone() == (0,)
        sector = con.execute(
            "SELECT sector, sector_source, sic, sic_source FROM securities WHERE ticker = 'AAPL'"
        ).fetchone()
        assert sector == ("Technology", "nasdaq", "3571", "sec")  # two taxonomies, two sources, never converted
        quote_sources = con.execute(
            "SELECT count(*), count(as_of) FROM sources WHERE dataset = 'equity.quote'"
        ).fetchone()
        assert quote_sources[0] == quote_sources[1] > 0  # every quote retrieval states the provider's own as-of
    finally:
        con.close()


def test_raw_payloads_are_saved_verbatim_next_to_their_source_rows(completed):
    rig, _ = completed
    files = sorted((rig.tmp_path / "raw" / "run1").glob("*.json.gz"))
    assert any(f.name.startswith("sec-companyfacts-CIK0000320193") for f in files)
    assert len(files) >= 14 * 2 + 13 * 2


def test_derived_metrics_follow_the_dictionary_on_the_stored_data(completed):
    _, report = completed
    by_cik = report["metrics"]

    def issuer(symbol_cik, name):
        return by_cik[symbol_cik]["issuer_metrics"][name]

    aapl = "0000320193"
    assert issuer(aapl, "revenue_growth_yoy")["state"] == "OK"
    assert issuer(aapl, "price_to_sales")["state"] == "OK" and issuer(aapl, "price_to_sales_ttm")["state"] == "OK"
    assert "PRICE_RETURN_EXCLUDES_DIVIDENDS" in by_cik[aapl]["listing_metrics"]["AAPL"]["price_return_1y"]["flags"]
    assert "total_return" not in by_cik[aapl]["listing_metrics"]["AAPL"]
    dd, dd_close = (
        by_cik[aapl]["listing_metrics"]["AAPL"][k]["value"]
        for k in ("drawdown_from_52w_high", "drawdown_from_52w_closing_high")
    )
    assert dd <= dd_close <= 0  # intraday-high drawdown is the deeper one: two distinct metrics
    rivn = "0001874178"
    assert issuer(rivn, "net_margin")["value"] == pytest.approx(-0.4)
    assert by_cik[rivn]["listing_metrics"]["RIVN"]["price_to_earnings"]["state"] == "NOT_MEANINGFUL"  # negative EPS
    assert issuer(rivn, "eps_growth_yoy")["state"] == "NOT_MEANINGFUL"
    nvda = "0001045810"
    assert issuer(nvda, "diluted_eps_ttm")["reason"] == "POSSIBLE_SPLIT_BASIS_CHANGE"
    assert by_cik[nvda]["listing_metrics"]["NVDA"]["price_to_earnings"]["state"] != "OK"
    brk = "0001067983"
    assert (
        issuer(brk, "total_debt")["state"] == "MISSING_INPUT"
        and "DEBT_COMPONENT_ABSENT" in issuer(brk, "total_debt")["reason"]
    )
    assert issuer(brk, "diluted_eps_ttm")["state"] == "MISSING_INPUT"
    jpm = "0000019617"
    assert (
        issuer(jpm, "fcf")["reason"] == "CAPEX_NOT_REPORTED" and issuer(jpm, "gross_margin")["state"] == "MISSING_INPUT"
    )
    assert "0001046179" not in by_cik or issuer("0001046179", "revenue_growth_yoy")["state"] == "MISSING_INPUT"


def test_market_cap_is_the_primary_listings_quote_not_a_sum(completed):
    rig, report = completed
    metrics = report["metrics"]["0001652044"]["issuer_metrics"]
    with access.reader(rig.db_path) as r:
        sid = r.find_securities_by_ticker("GOOGL")["security_id"][0]
        quoted = r.market_quotes(sid)["market_cap"][0]
    assert metrics["issuer_market_cap"]["value"] == quoted


# --- failure handling ----------------------------------------------------------------------------------------------


def test_without_sec_user_agent_nothing_is_fetched_or_written_and_the_run_says_why(rig):
    rig.user_agent = None
    run = rig.run()
    assert run.fatal == "SEC_USER_AGENT_MISSING"
    assert not rig.sec.requests and not rig.obb.calls
    assert all(count(rig.db_path, t) == 0 for t in ("securities", "price_daily", "financial_facts", "sources"))
    assert {s.status for s in run.stages.values()} == {"SKIPPED"}


def test_a_sequence_without_user_agent_reports_every_criterion_as_not_evaluable(rig):
    rig.user_agent = None
    report = run_p0_sequence(rig.db_path, lambda run_id: rig.make(run_id))
    assert report["summary"]["fatal"] == "SEC_USER_AGENT_MISSING"
    assert {c["status"] for c in report["acceptance"]} == {"NOT_EVALUABLE"} and len(report["acceptance"]) == 13
    assert report["verdict"]["overall"] == "NOT_RUN" and report["verdict"]["p1_gate"] == "CLOSED"
    assert report["verdict"]["pipeline_execution"]["status"] == "DID_NOT_RUN"


def test_sec_403_stops_all_sec_traffic_and_is_recorded_for_every_symbol(rig):
    rig.sec.forced["company_tickers_exchange"] = [403]
    run = rig.run()
    assert run.stages["identity"].status == "FAILED" and {i.kind for i in run.issues} == {"HTTP_403"}
    assert len(run.issues) == 14 and run.requests["http_403"] == 1 and len(rig.sec.requests) == 1
    assert not rig.obb.calls  # nothing downstream of a failed identity is fetched
    assert count(rig.db_path, "price_daily") == 0


def test_a_403_midway_blocks_the_remaining_sec_requests_but_keeps_what_was_stored(rig):
    rig.sec.forced["submissions/CIK0001045810"] = [403]  # NVDA's submissions
    run = rig.run()
    kinds = [(i.symbol, i.kind) for i in run.issues]
    assert ("NVDA", "HTTP_403") in kinds
    assert run.requests["http_403"] == 1
    assert run.outcomes["AAPL"].identity and run.outcomes["AAPL"].price  # issued before the block
    assert (
        count(rig.db_path, "price_daily") > 0 and count(rig.db_path, "financial_facts") == 0
    )  # facts come after the block


def test_429_is_retried_and_counted_then_the_run_continues(rig):
    rig.sec.forced["companyfacts/CIK0000320193"] = [429, 429]
    run = rig.run()
    assert run.requests["http_429"] == 2 and run.requests["retries"] == 2
    assert run.outcomes["AAPL"].facts and not any(i.kind == "HTTP_429" for i in run.issues)


def test_a_persistent_429_fails_that_issuer_only(rig):
    rig.sec.forced["companyfacts/CIK0000320193"] = [429] * 10
    run = rig.run()
    failures = [(i.symbol, i.kind) for i in run.issues if i.stage == "sec_facts"]
    assert failures == [("AAPL", "HTTP_429")]  # TSM is an expected gap, not a failure
    assert run.outcomes["NVDA"].facts and not run.outcomes["AAPL"].facts


def test_malformed_provider_document_is_recorded_and_other_issuers_are_unaffected(rig):
    rig.sec.forced["companyfacts/CIK0001045810"] = ["garbage"]
    run = rig.run()
    assert ("NVDA", "MALFORMED_RESPONSE") in [(i.symbol, i.kind) for i in run.issues]
    assert run.outcomes["AAPL"].facts and not run.outcomes["NVDA"].facts


def test_a_symbol_without_a_cik_is_reported_and_skipped_everywhere_downstream(rig):
    rig.sec.symbols = tuple(s for s in syn.CIKS if s != "KOSS")  # KOSS is not in the SEC map
    run = rig.run()
    issue = next(i for i in run.issues if i.symbol == "KOSS")
    assert issue.kind == "IDENTITY" and "not in the SEC ticker map" in issue.message
    assert not run.outcomes["KOSS"].identity and not run.outcomes["KOSS"].price and not run.outcomes["KOSS"].facts
    assert not any(c == "KOSS" for _, _, c in rig.obb.calls)  # never fetched without an identity
    assert count(rig.db_path, "securities") == 13


def test_a_cik_that_contradicts_the_phase_2_audit_is_a_failure_not_an_overwrite(rig):
    rig.sec.map_override = syn.ticker_map()
    rig.sec.map_override["data"] = [
        [999999 if row[2] == "AAPL" else row[0], *row[1:]] for row in rig.sec.map_override["data"]
    ]
    run = rig.run()
    assert any(i.symbol == "AAPL" and i.kind == "IDENTITY" and "audit recorded" in i.message for i in run.issues)
    assert not run.outcomes["AAPL"].identity


def test_one_failing_price_symbol_is_a_partial_batch_failure_not_an_aborted_run(rig):
    rig.obb.fail[("cboe", "AMD")] = RuntimeError("cboe down for AMD")
    rig.obb.fail[("nasdaq", "AMD")] = RuntimeError("nasdaq down for AMD")
    run = rig.run()
    assert run.stages["prices"].status == "PARTIAL" and run.stages["prices"].failed == 1
    assert ("AMD", "NETWORK") in [(i.symbol, i.kind) for i in run.issues]
    assert run.outcomes["AAPL"].price and not run.outcomes["AMD"].price
    assert count(rig.db_path, "price_daily") > 0 and run.outcomes["AMD"].facts  # its other data still loads


def test_primary_price_failure_falls_back_and_marks_the_source_as_fallback(rig):
    rig.obb.fail[("cboe", "KOSS")] = RuntimeError("cboe empty")
    run = rig.run()
    assert run.outcomes["KOSS"].price and run.outcomes["KOSS"].used_fallback_prices and run.fallback_retrievals == 1
    con = duckdb.connect(str(rig.db_path))
    try:
        row = con.execute("SELECT provider, is_fallback, detail FROM sources WHERE is_fallback").fetchall()
    finally:
        con.close()
    assert len(row) == 1 and row[0][0] == "nasdaq" and "primary cboe failed" in row[0][2]


def test_duplicate_and_implausible_provider_rows_are_counted_and_reported(rig):
    rig.obb.duplicate_rows.add("AAPL")
    rig.obb.bad_rows.add("AAPL")
    run = rig.run()
    categories = {w["category"] for w in run.warnings if w["symbol"] == "AAPL"}
    assert {"DUPLICATE_PROVIDER_ROWS", "REJECTED_PRICE_ROWS"} <= categories
    assert run.outcomes["AAPL"].price


def test_a_quote_failure_leaves_market_cap_missing_instead_of_guessed(rig):
    rig.obb.drop_quote_for.add("AAPL")
    run = rig.run()
    assert ("AAPL", "NETWORK") in [(i.symbol, i.kind) for i in run.issues if i.stage == "quotes"]
    snap = ms.compute_snapshots(rig.db_path, AS_OF)["0000320193"]
    assert snap.issuer["issuer_market_cap"].state is MetricState.MISSING_INPUT
    assert snap.issuer["price_to_sales"].state is MetricState.MISSING_INPUT  # never computed from another source


def test_a_busy_database_stops_the_run_before_any_network_use(rig, hold):
    with hold("lock-only"):
        run = rig.run()
    assert run.fatal == "DATABASE_LOCKED" and not rig.sec.requests and not rig.obb.calls
    assert {s.status for s in run.stages.values()} == {"SKIPPED"}


def test_a_database_error_rolls_back_the_entire_run(rig, monkeypatch):
    from app.database.write_repository import WriteRepository

    def explode(self, records):
        raise duckdb.ConstraintException("simulated constraint failure")

    monkeypatch.setattr(WriteRepository, "upsert_financial_facts", explode)
    run = rig.run()
    assert run.fatal == "DATABASE_ERROR" and "rolled back" in run.issues[-1].message
    assert all(count(rig.db_path, t) == 0 for t in ("securities", "price_daily", "market_quotes", "sources", "filings"))


# --- logical modification scenarios (idempotent upsert, never delete-and-reload) -----------------------------------


def test_one_changed_source_value_updates_exactly_one_row_and_a_new_accession_adds_one(rig):
    first = rig.run()
    base = first.totals()
    assert base.inserted > 0
    # the provider corrects ONE value of one fact in an existing filing
    doc = rig.sec.facts_doc("AAPL")
    points = doc["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
    point = next(p for p in points if p["end"] == "2025-09-27")  # the latest fiscal year
    point["val"] = point["val"] * 2
    rig.sec.docs_overrides["companyfacts/CIK0000320193"] = doc
    second = rig.run()
    assert second.tables["financial_facts"].updated == 1 and second.tables["financial_facts"].inserted == 0
    assert second.totals().inserted == 0 and second.totals().updated == 1
    con = duckdb.connect(str(rig.db_path))
    try:
        assert con.execute("SELECT count(*) FROM financial_facts WHERE value = ?", [point["val"]]).fetchone()[0] >= 1
        before = con.execute("SELECT count(*) FROM financial_facts").fetchone()[0]
    finally:
        con.close()
    # an amendment: the same period re-filed under a NEW accession is a new logical record, the original stays
    doc["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"].append(
        {**point, "accn": "0000320193-26-000099", "form": "10-K/A", "filed": "2026-02-04", "val": point["val"] + 1}
    )
    third = rig.run()
    assert third.tables["financial_facts"].inserted == 1 and third.tables["financial_facts"].updated == 0
    assert count(rig.db_path, "financial_facts") == before + 1
    snap = ms.compute_snapshots(rig.db_path, AS_OF)["0000320193"]
    assert snap.lines["net_income_to_common"]["form"] == "10-K/A"  # current view = latest vintage; the original is kept


def test_a_split_rebase_of_the_provider_history_is_detected_and_rewrites_rather_than_duplicates(rig):
    rig.run()
    rows_before = count(rig.db_path, "price_daily")
    original = rig.obb._bars

    def halved(symbol, start, end):
        rows = original(symbol, start, end)
        if symbol == "COST":
            for r in rows:
                for k in ("open", "high", "low", "close"):
                    r[k] = round(r[k] / 2, 4)
        return rows

    rig.obb._bars = halved
    run = rig.run()
    assert "COST" in run.split_rebases and run.split_rebases["COST"] == pytest.approx(0.5, rel=0.01)
    assert run.tables["price_daily"].updated > 300 and run.tables["price_daily"].inserted == 0
    assert count(rig.db_path, "price_daily") == rows_before


def test_user_agent_is_sent_on_every_sec_request(rig):
    rig.run()
    assert rig.sec.user_agents == {UA} and len(rig.sec.requests) == 27


def test_sec_request_volume_is_two_per_issuer_plus_the_ticker_map(rig):
    run = rig.run()
    assert run.requests["requests_by_provider"]["sec"] == 1 + 13 * 2
    assert run.requests["max_requests_in_any_second"]["sec"] >= 1
    assert isinstance(rig.sec.transport(), httpx.MockTransport)


# --- Phase 3A hardening: acceptance semantics, TSM/IFRS, warning attribution -------------------------------------


def evaluate_forced_live(rig, *, mutate_first=None, mutate_snapshots=None):
    """Run twice and evaluate with ``mode == LIVE``: this exercises the LIVE evaluation LOGIC on SIMULATED data.

    It proves how thresholds, numerators and denominators are computed. It is never evidence about a provider.
    """
    from app.ingestion import p0_report as pr

    first = rig.make("a").run()
    first.mode = "LIVE"
    if mutate_first:
        mutate_first(first)
    validation = ms.validation(rig.db_path)
    snapshots = ms.compute_snapshots(rig.db_path, AS_OF)
    if mutate_snapshots:
        mutate_snapshots(snapshots)
    coverage = ms.fiscal_year_coverage(rig.db_path, sorted(snapshots))
    second = rig.make("b").run()
    validation2 = ms.validation(rig.db_path)
    criteria = pr.evaluate(first, validation, snapshots, coverage, second=second, second_validation=validation2)
    return {c.id: c for c in criteria}, first


def comps(criterion):
    return {c.name: c for c in criterion.components}


def test_a12_without_nasdaq_revenue_is_partial_never_pass_and_keeps_the_other_checks(rig):
    criteria, _ = evaluate_forced_live(rig)
    a12 = criteria["A12"]
    parts = comps(a12)
    assert parts["week52_high_vs_nasdaq"].status == "PASS" and parts["market_cap_vs_close_x_shares"].status == "PASS"
    assert parts["revenue_vs_nasdaq"].status == "DEFERRED" and "not ingested" in parts["revenue_vs_nasdaq"].observed
    assert a12.status == "PARTIAL"  # two components were evaluated and passed; the third was not evaluated
    assert "Nasdaq revenue: not ingested" in a12.to_dict()["observed"]


def test_market_cap_check_reports_numerator_denominator_percentage_and_every_exclusion(rig):
    criteria, _ = evaluate_forced_live(rig)
    observed = comps(criteria["A12"])["market_cap_vs_close_x_shares"].observed
    assert "10/10 eligible single-class issuers within 2%" in observed and "100.0%" in observed
    assert "GOOGL/GOOG (MULTI_CLASS_ISSUER)" in observed and "BRK-B (MULTI_CLASS_ISSUER)" in observed
    assert "TSM (FACTS_UNAVAILABLE:UNSUPPORTED_TAXONOMY:ifrs-full)" in observed
    assert "3 excluded" in observed  # 13 issuers = 10 eligible + Alphabet + Berkshire + TSMC


def test_market_cap_pass_requires_at_least_90_percent_of_the_eligible_issuers(rig):
    rig.obb.cap_scale["AAPL"] = 1.5  # one of ten eligible issuers is off by 50%
    criteria, _ = evaluate_forced_live(rig)
    cap = comps(criteria["A12"])["market_cap_vs_close_x_shares"]
    assert "9/10" in cap.observed and "90.0%" in cap.observed and cap.status == "PASS"  # exactly the threshold
    rig.obb.cap_scale["NVDA"] = 0.5  # a second one: 8/10 = 80%
    criteria, _ = evaluate_forced_live(rig)
    cap = comps(criteria["A12"])["market_cap_vs_close_x_shares"]
    assert "8/10" in cap.observed and "80.0%" in cap.observed and cap.status == "FAIL"
    assert criteria["A12"].status == "FAIL"  # a measured miss is decisive even though revenue is deferred


def test_without_any_eligible_issuer_the_market_cap_check_is_not_evaluable_not_a_pass(rig):
    def nobody_is_eligible(snapshots):
        for snap in snapshots.values():
            snap.cross_checks["market_cap_check"] = {"eligible": False, "reason": "TEST", "ratio_minus_one": None}

    criteria, _ = evaluate_forced_live(rig, mutate_snapshots=nobody_is_eligible)
    cap = comps(criteria["A12"])["market_cap_vs_close_x_shares"]
    assert cap.status == "NOT_EVALUABLE" and cap.observed.startswith("0/0 eligible")
    assert criteria["A12"].status == "PARTIAL"


def test_no_criterion_is_pass_while_a_required_component_was_not_evaluated(rig, completed):
    """The brief's final check: over simulated AND forced-live evaluations, PASS implies every component PASS."""
    forced, _ = evaluate_forced_live(rig)
    _, report = completed
    simulated = {c["id"]: [k["status"] for k in c["components"]] for c in report["acceptance"]}
    live_like = {cid: [k.status for k in c.components] for cid, c in forced.items()}
    for statuses_by_id in (simulated, live_like):
        for cid, statuses in statuses_by_id.items():
            assert tuple(statuses) and len(statuses) == len(REQUIRED[cid])
    for c in report["acceptance"]:
        assert (c["status"] == "PASS") == all(k["status"] == "PASS" for k in c["components"]), c["id"]
    for cid, criterion in forced.items():
        assert (criterion.status == "PASS") == all(k.status == "PASS" for k in criterion.components), cid
    for cid in ("A3", "A4", "A7", "A10", "A11", "A12"):  # each has a component that is deferred or test-verified only
        assert forced[cid].status != "PASS", cid
    assert any(k.status in {"DEFERRED", "NOT_EVALUABLE"} for k in forced["A12"].components)


REQUIRED = __import__("app.ingestion.p0_report", fromlist=["REQUIRED_COMPONENTS"]).REQUIRED_COMPONENTS


def test_overall_verdict_on_the_pipeline_output_reconciles_execution_acceptance_and_limitations(completed):
    _, report = completed
    verdict = report["verdict"]
    assert verdict["pipeline_execution"]["status"] == "COMPLETED"
    assert verdict["pipeline_execution"]["genuine_failures"] == []
    assert [e["symbol"] for e in verdict["pipeline_execution"]["expected_unsupported"]] == ["TSM"]
    assert verdict["acceptance_criteria"]["status"] == "INCOMPLETE" and verdict["overall"] == "INCOMPLETE"
    assert verdict["p1_gate"] == "CLOSED" and verdict["p1_gate_reasons"]
    assert sum(verdict["acceptance_criteria"]["counts"].values()) == 13
    assert set(verdict["acceptance_criteria"]["counts"]) != {"PASS"}  # never "all PASS" with unevaluated components
    ids = {x["id"] for x in verdict["known_coverage_limitations"]}
    assert {"UNSUPPORTED_TAXONOMY:TSM", "NASDAQ_REVENUE_NOT_INGESTED", "TOTAL_ASSETS_NOT_IN_CONCEPT_SET"} <= ids
    assert report["summary"]["companies_succeeded_all_stages"] == 13
    assert (
        report["summary"]["companies_expected_unsupported"] == 1
        and report["summary"]["companies_failed_any_stage"] == 0
    )


def test_a_genuine_failure_in_the_sequence_fails_the_verdict(db_path, tmp_path):
    rig = Rig(db_path, tmp_path)
    rig.obb.fail[("cboe", "AMD")] = RuntimeError("down")
    rig.obb.fail[("nasdaq", "AMD")] = RuntimeError("down")
    report = run_p0_sequence(db_path, lambda run_id: rig.make(run_id))
    verdict = report["verdict"]
    assert verdict["pipeline_execution"]["status"] == "COMPLETED_WITH_FAILURES" and verdict["overall"] == "FAILED"
    failures = verdict["pipeline_execution"]["genuine_failures"]
    assert {f["symbol"] for f in failures} == {"AMD"} and {f["stage"] for f in failures} == {"prices", "quotes"}
    assert (
        report["summary"]["outcome_groups"]["failed"] == ["AMD"]
        and report["summary"]["companies_failed_any_stage"] == 1
    )


# --- TSM / IFRS: an explicit unsupported taxonomy, not a failure and not zero ---------------------------------------


def test_tsm_identity_prices_quotes_and_classification_load_but_no_facts_are_stored(completed):
    rig, _ = completed
    con = duckdb.connect(str(rig.db_path))
    try:
        cik = "0001046179"
        assert con.execute("SELECT count(*) FROM financial_facts WHERE cik = ?", [cik]).fetchone() == (0,)
        row = con.execute(
            "SELECT cik, sector, sector_source, sic_source FROM securities WHERE ticker = 'TSM'"
        ).fetchone()
        assert row == (cik, "Technology", "nasdaq", "sec")  # two taxonomies, each with its own source
        assert (
            con.execute(
                "SELECT count(*) FROM price_daily p JOIN securities s USING (security_id) WHERE s.ticker = 'TSM'"
            ).fetchone()[0]
            > 300
        )
        assert (
            con.execute(
                "SELECT count(*) FROM market_quotes q JOIN securities s USING (security_id) WHERE s.ticker = 'TSM'"
            ).fetchone()[0]
            == 1
        )
    finally:
        con.close()
    with access.reader(rig.db_path) as r:
        assert r.fact_support_notes("0001046179") == ["UNSUPPORTED_TAXONOMY:ifrs-full"]  # WHY there are no facts
        assert r.fact_support_notes("0000320193") == []


def test_tsm_fundamentals_are_missing_input_with_the_reason_never_zero_and_prices_still_work(completed):
    _, report = completed
    tsm = report["metrics"]["0001046179"]
    assert tsm["coverage"]["fundamentals"] == "UNAVAILABLE:UNSUPPORTED_TAXONOMY:ifrs-full"
    for name, result in tsm["issuer_metrics"].items():
        if name == "issuer_market_cap":
            continue  # market data, not a fundamental
        assert result["state"] == "MISSING_INPUT" and result["value"] is None, name
        assert result["reason"] == "UNSUPPORTED_TAXONOMY:ifrs-full", name
    assert tsm["lines"] == {"_fundamentals": {"value": None, "reason": "UNSUPPORTED_TAXONOMY:ifrs-full"}}
    listing = tsm["listing_metrics"]["TSM"]
    assert listing["price_to_earnings"]["state"] != "OK" and listing["price_to_earnings"]["value"] is None
    assert listing["price_return_1y"]["state"] == "OK"  # price screens still work
    assert tsm["issuer_metrics"]["issuer_market_cap"]["state"] == "OK"
    check = tsm["cross_checks"]["market_cap_check"]
    assert check["eligible"] is False and check["reason"] == "FACTS_UNAVAILABLE:UNSUPPORTED_TAXONOMY:ifrs-full"


def test_the_rerun_treats_tsm_identically_and_stays_idempotent(completed):
    _, report = completed
    idem = report["idempotency"]
    assert idem["expected_unsupported_run1"] == idem["expected_unsupported_run2"] == ["TSM"]
    assert (idem["run1"]["issues"], idem["run2"]["issues"]) == (0, 0)
    assert (idem["run2"]["inserted"], idem["run2"]["updated"]) == (0, 0)
    assert report["second_run"]["stages"]["sec_facts"]["expected_unsupported"] == 1


def test_a_document_with_us_gaap_is_never_classified_as_unsupported_and_empty_us_gaap_is_still_a_failure(rig):
    doc = {"cik": syn.CIKS["KOSS"], "entityName": "X", "facts": {"us-gaap": {"SomethingUnlisted": {"units": {}}},
                                                               "ifrs-full": {}}}  # fmt: skip
    rig.sec.docs_overrides["companyfacts/CIK0000056701"] = doc
    run = rig.run()
    assert [(i.symbol, i.kind) for i in run.issues] == [("KOSS", "NO_ALLOWLISTED_FACTS")]  # genuine, not expected
    assert run.expected_unsupported and [e["symbol"] for e in run.expected_unsupported] == ["TSM"]
    assert run.outcomes["KOSS"].facts_status == "FAILED"


# --- warning attribution ----------------------------------------------------------------------------------------


def by(run, category, symbol):
    return [w for w in run.warnings if w["category"] == category and w["symbol"] == symbol]


def test_identical_duplicate_rows_are_attributed_to_ticker_provider_stage_and_dates(rig):
    rig.obb.duplicate_rows.add("AAPL")
    run = rig.run()
    (w,) = by(run, "DUPLICATE_PROVIDER_ROWS", "AAPL")
    assert (w["provider"], w["stage"]) == ("cboe", "prices")
    assert w["details"]["duplicates"] == 3 and w["details"]["conflicting"] == 0 and len(w["details"]["dates"]) == 3
    assert w["details"]["command"] == "obb.cboe.equity.historical" and w["details"]["sent_symbol"] == "AAPL"
    assert "all identical" in w["message"]
    con = duckdb.connect(str(rig.db_path))
    try:  # the de-duplication itself is unchanged: one stored row per date
        assert con.execute(
            "SELECT max(n) FROM (SELECT count(*) n FROM price_daily GROUP BY security_id, trade_date)"
        ).fetchone() == (1,)
    finally:
        con.close()


def test_duplicates_with_different_values_get_their_own_category(rig):
    rig.obb.conflicting_duplicates.add("COST")
    run = rig.run()
    assert not by(run, "DUPLICATE_PROVIDER_ROWS", "COST")
    (w,) = by(run, "CONFLICTING_DUPLICATE_PROVIDER_ROWS", "COST")
    assert w["details"]["conflicting"] == 1 and w["details"]["samples"][0]["identical"] is False
    assert "DIFFERENT values" in w["message"]


def test_rejected_price_rows_name_the_row_the_reason_and_the_provider(rig):
    rig.obb.bad_rows.add("NVDA")
    run = rig.run()
    (w,) = by(run, "REJECTED_PRICE_ROWS", "NVDA")
    assert w["provider"] == "cboe" and w["details"]["reason"] == "HIGH_BELOW_LOW"
    assert w["details"]["rows"][0]["date"] == "2025-01-02" and w["stage"] == "prices"


def test_a_missing_nasdaq_classification_is_attributed_and_stores_nothing_invented(rig):
    rig.obb.no_classification.add("META")
    run = rig.run()
    (w,) = by(run, "NO_NASDAQ_CLASSIFICATION", "META")
    assert (w["provider"], w["stage"]) == ("nasdaq", "quotes")
    assert w["details"]["raw_sector"] is None and w["details"]["raw_industry"] is None
    con = duckdb.connect(str(rig.db_path))
    try:
        assert con.execute(
            "SELECT sector, industry, sector_source FROM securities WHERE ticker = 'META'"
        ).fetchone() == (
            None,
            None,
            None,
        )
    finally:
        con.close()


def test_warnings_from_the_fallback_provider_say_so(rig):
    rig.obb.fail[("cboe", "KOSS")] = RuntimeError("cboe empty")
    rig.obb.duplicate_rows.add("KOSS")
    run = rig.run()
    (w,) = by(run, "DUPLICATE_PROVIDER_ROWS", "KOSS")
    assert w["provider"] == "nasdaq" and w["details"]["is_fallback"] is True


def test_python_warnings_raised_inside_openbb_are_captured_and_attributed(rig):
    rig.obb.warn_on[("cboe", "historical")] = "endpoint is deprecated"
    run = rig.run()
    captured = [w for w in run.warnings if w["category"] == "OPENBB_WARNING"]
    assert {w["symbol"] for w in captured} >= {"AAPL", "KOSS", "TSM"} and len(captured) >= 13
    sample = next(w for w in captured if w["symbol"] == "AAPL")
    assert (
        sample["provider"] == "cboe" and sample["stage"] == "prices" and sample["message"] == "endpoint is deprecated"
    )
    assert sample["details"]["warning_class"] == "UserWarning" and sample["details"]["command"].startswith("obb.cboe")


def test_every_warning_is_attributable_and_an_unattributed_one_fails_the_criterion(rig):
    rig.obb.duplicate_rows.add("AAPL")
    rig.obb.no_classification.add("META")
    rig.obb.warn_on[("cboe", "historical")] = "w"
    criteria, run = evaluate_forced_live(rig)
    parts = comps(criteria["A9"])
    assert parts["warnings_attributable"].status == "PASS" and parts["warnings_categorised"].status == "PASS"
    assert all(w["symbol"] and w["provider"] and w["stage"] for w in run.warnings)

    def inject(first):
        first.warnings.append({"category": "MYSTERY", "symbol": None, "provider": None, "stage": None, "message": "?"})

    criteria, _ = evaluate_forced_live(rig, mutate_first=inject)
    parts = comps(criteria["A9"])
    assert parts["warnings_attributable"].status == "FAIL" and parts["warnings_categorised"].status == "FAIL"
    assert criteria["A9"].status == "FAIL"


def test_the_markdown_report_shows_the_verdict_the_components_and_the_attributed_warnings(completed):
    from app.ingestion.p0_report import render_markdown

    _, report = completed
    text = render_markdown(report)
    assert "**Overall: INCOMPLETE**" in text and "Pipeline execution: COMPLETED" in text
    assert "Expected unsupported coverage (not failures)" in text and "TSM" in text and "UNSUPPORTED_TAXONOMY" in text
    assert "`revenue_vs_nasdaq` **DEFERRED**" in text and "Provider warnings (attributed)" in text
    assert "UNSUPPORTED_TAXONOMY:TSM" in text and "NASDAQ_REVENUE_NOT_INGESTED" in text
