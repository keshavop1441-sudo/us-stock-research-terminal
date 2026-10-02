"""The P0 data-quality report: measurements M1-M12 and the comparison with the Phase 2 acceptance criteria A1-A13.

Pure functions over the run reports, the validation output and the metric snapshots. Statuses are PASS / FAIL /
NOT_EVALUATED, and NOT_EVALUATED is used wherever the criterion needs a measurement that was not made (for example every
provider-behaviour criterion when the providers were simulated). Nothing is rounded up into a pass.
"""

import math
from collections import Counter

from app.ingestion.acceptance import (
    Component,
    Criterion,
    component,
    not_run,
    overall_verdict,
)
from app.ingestion.manifest import MANIFEST
from app.ingestion.run_report import RunReport
from app.screening.snapshot import CORE_LINES, IssuerSnapshot

KNOWN_WARNING_CATEGORIES = frozenset({
    "REJECTED_PRICE_ROWS", "DUPLICATE_PROVIDER_ROWS", "CONFLICTING_DUPLICATE_PROVIDER_ROWS", "SPLIT_REBASE_DETECTED",
    "NO_NASDAQ_CLASSIFICATION", "NO_QUOTED_MARKET_CAP", "REJECTED_FACT_POINT", "DUPLICATE_FACT_POINTS",
    "TICKER_NOT_IN_SUBMISSIONS", "OPENBB_WARNING",
})  # fmt: skip
# every failure class the pipeline can record has a handler (a catch clause that records it and moves on)
HANDLED_ISSUE_KINDS = frozenset({
    "HTTP_403", "HTTP_429", "HTTP_404", "HTTP_5XX", "NETWORK", "MALFORMED_RESPONSE", "EMPTY_DATA", "IDENTITY",
    "NO_ALLOWLISTED_FACTS", "INVALID_RECORD", "ERROR", "SEC_USER_AGENT_MISSING", "DATABASE_LOCKED",
    "DATABASE_ERROR", "UNSUPPORTED_TAXONOMY",
})  # fmt: skip
# the REQUIRED components of each criterion; ``evaluate`` refuses to return a criterion that does not list exactly these
REQUIRED_COMPONENTS = {
    "A1": ("identity_rate", "ambiguous_tickers"),
    "A2": ("round_trip", "unnormalised_ciks"),
    "A3": ("price_coverage", "duplicate_price_keys", "gaps_over_5_business_days", "split_rebase_no_false_positive",
           "split_rebase_true_detection"),
    "A4": ("industrial_four_fiscal_years", "total_assets"),
    "A5": ("core_fields_missing", "zero_debt_without_evidence", "non_finite_stored"),
    "A6": ("duplicates_after_run1", "duplicates_after_run2"),
    "A7": ("sec_sustained_rate", "requests_per_security_recorded", "full_universe_projection"),
    "A8": ("failing_request_rate", "sec_403_429", "failure_classes_handled"),
    "A9": ("warnings_categorised", "warnings_attributable"),
    "A10": ("no_manual_intervention", "per_security_p95_recorded", "extrapolation_meets_a7"),
    "A11": ("rerun_inserts_nothing", "changed_fact_updates_one", "amendment_adds_accession",
            "reads_honest_during_write"),
    "A12": ("week52_high_vs_nasdaq", "market_cap_vs_close_x_shares", "revenue_vs_nasdaq"),
    "A13": ("rows_carry_source_id", "sec_facts_carry_accession_filed_form", "classification_has_source"),
}  # fmt: skip
PROVIDER_ERROR_KINDS = frozenset({
    "HTTP_403", "HTTP_429", "HTTP_404", "HTTP_5XX", "NETWORK", "MALFORMED_RESPONSE", "EMPTY_DATA",
})  # fmt: skip
SEC_MAX_PER_SECOND = 9
_ROLES = {s.symbol: set(s.roles) for s in MANIFEST}
_NOT_INDUSTRIAL = {"financial", "adr"}  # reported separately from the industrial-filer thresholds (YAML A4/A5)
_ZERO_DEBT_LINES = ("short_term_debt", "current_portion_long_term_debt", "long_term_debt")
SIMULATED_NOTE = "providers were simulated: provider-behaviour components are not evaluated"
MECHANICAL_NOTE = "mechanical check of the write path"


def _pct(n: int, d: int) -> float | None:
    return None if d == 0 else n / d


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def _roles_of_cik(run: RunReport, cik: str) -> set[str]:
    roles: set[str] = set()
    for symbol, outcome in run.outcomes.items():
        if outcome.cik == cik:
            roles |= _ROLES[symbol]
    return roles


def _industrial(run: RunReport, snapshots: dict[str, IssuerSnapshot]) -> list[str]:
    return [cik for cik in snapshots if not _roles_of_cik(run, cik) & _NOT_INDUSTRIAL]


def _missing_cells(snapshots: dict[str, IssuerSnapshot], ciks: list[str]) -> tuple[list, list]:
    cells = [(cik, line) for cik in ciks for line in CORE_LINES]
    missing = [(cik, line) for cik, line in cells if snapshots[cik].lines.get(line, {}).get("value") is None]
    return cells, missing


def _duplicates(validation: dict | None) -> int | None:
    if validation is None:
        return None
    return sum(v for k, v in validation["integrity"].items() if k.startswith("duplicate_"))


def outcome_groups(run: RunReport) -> dict[str, list[str]]:
    """Securities by what happened: succeeded in every stage / expected-unsupported (only the facts stage) / failed."""
    groups: dict[str, list[str]] = {"succeeded": [], "expected_unsupported": [], "failed": []}
    for symbol, o in run.outcomes.items():
        core_ok = o.identity and o.price and o.quote
        if core_ok and o.facts:
            groups["succeeded"].append(symbol)
        elif core_ok and o.facts_status == "UNSUPPORTED_TAXONOMY":
            groups["expected_unsupported"].append(symbol)
        else:
            groups["failed"].append(symbol)
    return groups


def summarise(run: RunReport, validation: dict, snapshots: dict[str, IssuerSnapshot], fy_coverage: dict) -> dict:
    """The headline measurements of ONE run (the fields the brief lists)."""
    outcomes = list(run.outcomes.values())
    attempted = len(outcomes)
    identified = sum(o.identity for o in outcomes)
    groups = outcome_groups(run)
    fact_attempts = Counter(o.facts_status for o in outcomes if o.identity)
    fact_denominator = identified - fact_attempts.get("UNSUPPORTED_TAXONOMY", 0)
    states: Counter = Counter()
    for snap in snapshots.values():
        for _, result in snap.all_results():
            states[result.state.value] += 1
    all_cells, all_missing = _missing_cells(snapshots, list(snapshots))
    industrial_cells, industrial_missing = _missing_cells(snapshots, _industrial(run, snapshots))
    mismatched = sum(1 for w in run.warnings if w["category"] == "TICKER_NOT_IN_SUBMISSIONS")
    requests = run.requests or {}
    seconds = [o.seconds for o in outcomes if o.seconds]
    return {
        "run_id": run.run_id,
        "mode": run.mode,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "runtime_seconds": run.runtime_seconds,
        "fatal": run.fatal,
        "stages": {
            n: {
                "status": s.status,
                "attempted": s.attempted,
                "succeeded": s.succeeded,
                "failed": s.failed,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
                "seconds": s.seconds,
            }
            for n, s in run.stages.items()
        },  # fmt: skip
        "securities_attempted": attempted,
        "issuers_resolved": len({o.cik for o in outcomes if o.cik}),
        "companies_succeeded_all_stages": len(groups["succeeded"]),
        "companies_expected_unsupported": len(groups["expected_unsupported"]),
        "companies_failed_any_stage": len(groups["failed"]),
        "outcome_groups": groups,
        "identity_success_rate": _pct(identified, attempted),
        "cik_mapping_success": _pct(sum(1 for o in outcomes if o.cik) - mismatched, attempted),
        "price_success": _pct(sum(o.price for o in outcomes if o.identity), identified),
        "quote_success": _pct(sum(o.quote for o in outcomes if o.identity), identified),
        "sec_fact_success": _pct(sum(o.facts for o in outcomes if o.identity), fact_denominator),
        "sec_fact_outcomes": dict(fact_attempts),
        "sec_fact_denominator_note": "denominator excludes issuers with an unsupported taxonomy (listed apart)",
        "missing_core_field_rate_all": _pct(len(all_missing), len(all_cells)),
        "missing_core_field_rate_industrial": _pct(len(industrial_missing), len(industrial_cells)),
        "metric_states": dict(states),
        "stored_duplicate_keys": _duplicates(validation),
        "provider_duplicate_rows": sum(c.duplicates for c in run.tables.values()),
        "inserted": run.totals().inserted,
        "updated": run.totals().updated,
        "unchanged": run.totals().unchanged,
        "failed": len(run.issues),
        "retrievals_appended_to_sources": run.retrievals_recorded,
        "fallback_retrievals": run.fallback_retrievals,
        "request_count": requests.get("requests_total"),
        "requests_by_provider": requests.get("requests_by_provider"),
        "request_rate_overall_per_second": requests.get("requests_per_second_overall"),
        "max_requests_in_any_second": requests.get("max_requests_in_any_second"),
        "http_403": requests.get("http_403"),
        "http_429": requests.get("http_429"),
        "retries": requests.get("retries"),
        "bytes_received": requests.get("bytes_received"),
        "provider_warnings": len(run.warnings),
        "warning_categories": dict(Counter(str(w["category"]) for w in run.warnings)),
        "warning_breakdown": dict(
            Counter(f"{w['category']} | {w.get('provider')} | {w.get('symbol')}" for w in run.warnings)
        ),
        "expected_unsupported": run.expected_unsupported,
        "database_size_bytes": run.db_size_bytes,
        "per_security_seconds_median": sorted(seconds)[len(seconds) // 2] if seconds else None,
        "per_security_seconds_p95": p95(seconds),
        "fy_coverage": fy_coverage,
    }


def _label(run: RunReport, cik: str) -> str:
    return "/".join(s for s, o in run.outcomes.items() if o.cik == cik) or cik


def _a1_a2(run: RunReport, validation: dict, live: bool, note: str) -> list[Criterion]:
    outcomes = list(run.outcomes.values())
    n = len(outcomes)
    identified = sum(o.identity for o in outcomes)
    ambiguous = sum(1 for i in run.issues if i.kind == "IDENTITY" and "ambiguous" in i.message)
    mismatched = sum(1 for w in run.warnings if w["category"] == "TICKER_NOT_IN_SUBMISSIONS")
    unnormalised = validation["integrity"]["unnormalised_ciks"]
    rate = identified / n if n else None
    return [
        Criterion(
            "A1", "M1 identity", ">= 99% resolved; 0 ambiguous",
            [
                component("identity_rate", (rate >= 0.99) if live and rate is not None else None,
                          f"{identified}/{n} ({_fmt(rate)}); with 14 securities >= 99% means all 14", note),
                component("ambiguous_tickers", (ambiguous == 0) if live else None, f"{ambiguous} ambiguous", note),
            ],
        ),
        Criterion(
            "A2", "M2 CIK mapping", "100% round-trip ticker->CIK->ticker; 0 un-normalised CIKs",
            [
                component("round_trip", (mismatched == 0 and identified > 0) if live else None,
                          f"{identified - mismatched}/{identified} round-trip", note),
                component("unnormalised_ciks", unnormalised == 0, f"{unnormalised} un-normalised", MECHANICAL_NOTE),
            ],
        ),
    ]  # fmt: skip


def _a3(run: RunReport, second: RunReport | None, validation: dict, live: bool, note: str) -> Criterion:
    priced = [o for o in run.outcomes.values() if o.identity]
    covered = [
        o for o in priced
        if o.price and not o.price_gaps and o.price_last and (run.as_of - o.price_last).days <= 5
    ]  # fmt: skip
    gaps = {o.symbol: [(str(a), str(b), g) for a, b, g in o.price_gaps] for o in priced if o.price_gaps}
    duplicate_keys = validation["integrity"]["duplicate_price_keys"]
    coverage = len(covered) / len(priced) if priced else None
    return Criterion(
        "A3", "M3 prices",
        ">= 98% with full expected coverage; 0 duplicate (security_id, date); gaps > 5 trading days reviewed; "
        "split re-base detected",
        [
            component("price_coverage", (coverage >= 0.98) if live and coverage is not None else None,
                      f"{len(covered)}/{len(priced)} fully covered (no gap over 5 business days, last bar within 5 "
                      "days of the run date)", note),
            component("duplicate_price_keys", duplicate_keys == 0, f"{duplicate_keys} duplicate keys", MECHANICAL_NOTE),
            component("gaps_over_5_business_days", (not gaps) if live else None,
                      f"{gaps or 'none'}",
                      note or "a gap needs a human review (exchange holidays are not removed): listed, not judged"),
            component("split_rebase_no_false_positive", (not second.split_rebases) if second else None,
                      f"re-bases on the identical re-run: {(second.split_rebases if second else 'no second run')}",
                      "re-run of unchanged data must not look like a split"),
            component("split_rebase_true_detection", None,
                      f"re-bases seen in this run: {run.split_rebases or 'none'}",
                      "needs a refresh that spans a real split; verified only by the hermetic test "
                      "test_a_split_rebase_of_the_provider_history_is_detected_and_rewrites_rather_than_duplicates",
                      deferred=True),
        ],
    )  # fmt: skip


def _a4_a5(run, validation, snapshots, fy_coverage, live: bool, note: str) -> list[Criterion]:
    industrial = _industrial(run, snapshots)
    full = [c for c in industrial if all(v >= 4 for v in fy_coverage.get(c, {"x": 0}).values())]
    cells, missing = _missing_cells(snapshots, industrial)
    zero_debt_unevidenced = 0
    for s in snapshots.values():
        debt = s.issuer["total_debt"]
        explicit_zero = all(s.lines.get(k, {}).get("value") == 0 for k in _ZERO_DEBT_LINES)
        if debt.ok and debt.value == 0 and "EXPLICIT_NO_DEBT_EVIDENCE" not in debt.flags and not explicit_zero:
            zero_debt_unevidenced += 1
    integrity = validation["integrity"]
    nonfinite = integrity["non_finite_prices"] + integrity["non_finite_facts"]
    rate = _pct(len(missing), len(cells))
    apart = []
    for cik, snap in snapshots.items():
        if cik in industrial:
            continue
        label = _label(run, cik)
        coverage = snap.coverage.get("fundamentals", "?")
        apart.append(f"{label}: {sorted(_roles_of_cik(run, cik) & _NOT_INDUSTRIAL)} reported apart "
                     f"(fundamentals {coverage}; fiscal years {fy_coverage.get(cik)})")  # fmt: skip
    return [
        Criterion(
            "A4", "M4 SEC facts",
            ">= 95% of non-financial operating companies have revenue, net income to common, operating cash flow, "
            "total assets for >= 4 fiscal years",
            [
                component("industrial_four_fiscal_years",
                          (len(full) / len(industrial) >= 0.95) if live and industrial else None,
                          f"{len(full)}/{len(industrial)} non-financial us-gaap issuers (revenue, net income, "
                          "operating cash flow)", note),
                component("total_assets", None, "not ingested",
                          "total assets is not in the P0 concept set (brief section 9)", deferred=True),
            ],
            informational=apart,
        ),
        Criterion(
            "A5", "M5 missing data",
            "core fields missing <= 5% (industrial); 0 debt values of 0 without explicit evidence; 0 NaN/inf stored",
            [
                component("core_fields_missing", (rate is not None and rate <= 0.05) if live else None,
                          f"{len(missing)}/{len(cells)} core cells missing ({_fmt(rate)})",
                          note or f"missing cells: {sorted(missing)}"),
                component("zero_debt_without_evidence", (zero_debt_unevidenced == 0) if live else None,
                          f"{zero_debt_unevidenced} unevidenced zero-debt values", note),
                component("non_finite_stored", nonfinite == 0, f"{nonfinite} NaN/inf stored", MECHANICAL_NOTE),
            ],
        ),
    ]  # fmt: skip


def _a7_a10(run: RunReport, live: bool, note: str) -> list[Criterion]:
    outcomes = list(run.outcomes.values())
    requests = run.requests or {}
    peak = (requests.get("max_requests_in_any_second") or {}).get("sec")
    sec_total = (requests.get("requests_by_provider") or {}).get("sec")
    issuers = len({o.cik for o in outcomes if o.cik})
    per_issuer = f"{sec_total / issuers:.1f}" if sec_total and issuers else "n/a"
    req_total = requests.get("requests_total") or 0
    failed = sum(1 for i in run.issues if i.kind in PROVIDER_ERROR_KINDS)
    h403, h429 = requests.get("http_403", 0), requests.get("http_429", 0)
    uncategorised = [w for w in run.warnings if w["category"] not in KNOWN_WARNING_CATEGORIES]
    unattributed = [w for w in run.warnings if not (w.get("symbol") and w.get("provider") and w.get("stage"))]
    unhandled = sorted({i.kind for i in run.issues} - HANDLED_ISSUE_KINDS)
    seconds = [o.seconds for o in outcomes if o.seconds]
    return [
        Criterion(
            "A7", "M7 requests",
            f"SEC sustained <= {SEC_MAX_PER_SECOND}/s; requests per security recorded; full-universe projection OK",
            [
                component("sec_sustained_rate", (peak is not None and peak <= SEC_MAX_PER_SECOND) if live else None,
                          f"SEC peak {peak}/s over {sec_total} SEC requests", note),
                component("requests_per_security_recorded", sec_total is not None and bool(issuers),
                          f"{per_issuer} SEC requests per issuer (incl. the one-off ticker map)", MECHANICAL_NOTE),
                component("full_universe_projection", None, "not projected",
                          "initial-load (<= 12 h) and daily-incremental (<= 30 min) projections need P1 measurements",
                          deferred=True),
            ],
        ),
        Criterion(
            "A8", "M8 failures", "<= 1% of requests failing after retries; 0 SEC 403/429; every failure class handled",
            [
                component("failing_request_rate", (failed <= 0.01 * req_total) if live and req_total else None,
                          f"{failed} failed operations / {req_total} requests", note),
                component("sec_403_429", (h403 == 0 and h429 == 0) if live else None, f"403={h403}; 429={h429}", note),
                component("failure_classes_handled", not unhandled,
                          f"unhandled failure kinds: {unhandled or 'none'}", MECHANICAL_NOTE),
            ],
        ),
        Criterion(
            "A9", "M9 warnings", "every provider warning categorised; 0 uncategorised",
            [
                component("warnings_categorised", (not uncategorised) if live else None,
                          f"{len(run.warnings)} warnings, {len(uncategorised)} uncategorised "
                          "(Python warnings raised inside OpenBB calls are captured as OPENBB_WARNING)", note),
                component("warnings_attributable", not unattributed,
                          f"{len(unattributed)} warnings lack a ticker, provider or stage",
                          "each warning must identify the affected security and provider"),
            ],
        ),
        Criterion(
            "A10", "M10 time",
            "completes without manual intervention; per-security p95 recorded; extrapolation meets A7",
            [
                component("no_manual_intervention", (run.fatal is None) if live else None,
                          f"fatal={run.fatal}; runtime {run.runtime_seconds}s", note),
                component("per_security_p95_recorded", bool(seconds), f"p95 {p95(seconds)}s", MECHANICAL_NOTE),
                component("extrapolation_meets_a7", None, "not extrapolated",
                          "extrapolation to the full universe is a P1/P2 question", deferred=True),
            ],
        ),
    ]  # fmt: skip


def _a11(first: RunReport, second: RunReport | None) -> Criterion:
    t = second.totals() if second else None
    tests = (
        "verified by hermetic tests (tests/test_p0_pipeline.py, tests/test_upsert_accounting.py, "
        "tests/test_services.py)"
    )
    return Criterion(
        "A11", "M11 upsert",
        "second identical run inserts 0 rows; one changed fact updates exactly 1; an amended filing adds a new "
        "accession row; reads during a write are honest",
        [
            component("rerun_inserts_nothing",
                      (t.inserted == 0 and t.updated == 0 and not second.fatal) if second and t else None,
                      f"run 2: inserted {t.inserted}, updated {t.updated}, unchanged {t.unchanged}, "
                      f"failed {len(second.issues)}" if second and t else "no second run"),
            component("changed_fact_updates_one", None, "not exercised by the live run", tests),
            component("amendment_adds_accession", None, "not exercised by the live run", tests),
            component("reads_honest_during_write", None, "not exercised by the live run", tests),
        ],
    )  # fmt: skip


def _a12_a13(run: RunReport, snapshots, validation, live: bool, note: str) -> list[Criterion]:
    year_high = [(k.split(":")[0], abs(v)) for s in snapshots.values() for k, v in s.cross_checks.items()
                 if k.endswith(":year_high_diff")]  # fmt: skip
    hi_ok = sum(v <= 0.005 for _, v in year_high)
    hi_rate = hi_ok / len(year_high) if year_high else None
    eligible, excluded = [], []
    for cik, snap in snapshots.items():
        check = snap.cross_checks.get("market_cap_check") or {"eligible": False, "reason": "NOT_COMPUTED"}
        (eligible if check["eligible"] else excluded).append((_label(run, cik), check))
    cap_ok = sum(abs(c["ratio_minus_one"]) <= 0.02 for _, c in eligible)
    cap_rate = cap_ok / len(eligible) if eligible else None
    excluded_text = ", ".join(f"{label} ({c['reason']})" for label, c in excluded) or "none"
    cap_observed = (
        f"{cap_ok}/{len(eligible)} eligible single-class issuers within 2% of close x dei shares "
        f"({_fmt(cap_rate)}; need >= 90%); {len(excluded)} excluded: {excluded_text}"
    )
    integrity = validation["integrity"]
    no_source = sum(integrity[k] for k in (
        "prices_without_source", "facts_without_source", "filings_without_source", "quotes_without_source",
    ))  # fmt: skip
    return [
        Criterion(
            "A12", "M12 cross-checks",
            "52w high within 0.5% of Nasdaq year_high for >= 95%; issuer_market_cap within 2% of close x dei shares "
            "for >= 90% of single-class issuers; revenue within 1% of Nasdaq",
            [
                component("week52_high_vs_nasdaq", (hi_rate >= 0.95) if live and hi_rate is not None else None,
                          f"{hi_ok}/{len(year_high)} listings within 0.5% ({_fmt(hi_rate)}; need >= 95%)", note),
                component("market_cap_vs_close_x_shares", (cap_rate >= 0.90) if live and cap_rate is not None else None,
                          cap_observed, note or ("no eligible single-class issuer" if not eligible else "")),
                component("revenue_vs_nasdaq", None, "Nasdaq revenue: not ingested",
                          "Nasdaq statements are outside P0; this portion is not measured and not counted as passed",
                          deferred=True),
            ],
        ),
        Criterion(
            "A13", "M5 provenance",
            "100% of provider rows carry source_id; 100% of SEC fact rows carry accession_no, filed_date, form; "
            "classification stored with source",
            [
                component("rows_carry_source_id", no_source == 0, f"{no_source} rows without source_id",
                          MECHANICAL_NOTE),
                component("sec_facts_carry_accession_filed_form",
                          integrity["facts_without_accession_filed_or_form"] == 0,
                          f"{integrity['facts_without_accession_filed_or_form']} fact rows lacking one of them",
                          MECHANICAL_NOTE),
                component("classification_has_source",
                          integrity["classification_without_source"] == 0
                          and integrity["sources_missing_command_or_parameters"] == 0
                          and integrity["sources_missing_content_hash"] == 0,
                          f"{integrity['classification_without_source']} classified securities without a source; "
                          f"{integrity['sources_missing_command_or_parameters']} retrievals without command/parameters",
                          MECHANICAL_NOTE),
            ],
        ),
    ]  # fmt: skip


def evaluate(
    first: RunReport,
    first_validation: dict,
    snapshots: dict[str, IssuerSnapshot],
    fy_coverage: dict,
    *,
    second: RunReport | None = None,
    second_validation: dict | None = None,
) -> list[Criterion]:
    live = first.mode == "LIVE" and first.fatal is None
    note = "" if first.mode == "LIVE" else SIMULATED_NOTE
    dup1, dup2 = _duplicates(first_validation), _duplicates(second_validation)
    a6 = Criterion(
        "A6", "M6 duplicates", "0 duplicate logical keys in every table after load and after a re-run",
        [
            component("duplicates_after_run1", dup1 == 0, f"{dup1} duplicate keys", MECHANICAL_NOTE),
            component("duplicates_after_run2", (dup2 == 0) if dup2 is not None else None,
                      f"{dup2 if dup2 is not None else 'no second run'} duplicate keys", MECHANICAL_NOTE),
        ],
    )  # fmt: skip
    criteria = [
        *_a1_a2(first, first_validation, live, note),
        _a3(first, second, first_validation, live, note),
        *_a4_a5(first, first_validation, snapshots, fy_coverage, live, note),
        a6,
        *_a7_a10(first, live, note),
        _a11(first, second),
        *_a12_a13(first, snapshots, first_validation, live, note),
    ]
    for criterion in criteria:  # a required component can never be dropped silently
        expected = REQUIRED_COMPONENTS[criterion.id]
        assert tuple(c.name for c in criterion.components) == expected, (criterion.id, expected)
    return criteria


def not_run_criteria(reason: str) -> list[Criterion]:
    return [not_run(f"A{i}", reason) for i in range(1, 14)]


def verdict(
    first: RunReport, criteria: list[Criterion], summary: dict, second: RunReport | None = None
) -> dict[str, object]:
    return overall_verdict(
        run_fatal=first.fatal,
        genuine_failures=[
            {"stage": i.stage, "symbol": i.symbol, "kind": i.kind, "message": i.message} for i in first.issues
        ],  # fmt: skip
        expected_unsupported=first.expected_unsupported,
        securities_attempted=summary.get("securities_attempted", len(first.symbols)),
        succeeded_all_stages=summary.get("companies_succeeded_all_stages", 0),
        criteria=criteria,
        second_run_failures=None if second is None else len(second.issues),
    )


__all__ = ["Component", "Criterion", "evaluate", "not_run_criteria", "summarise", "verdict"]


def _table(rows: list[tuple[str, object]]) -> list[str]:
    return ["| measurement | value |", "|---|---|", *(f"| {k} | {v} |" for k, v in rows)]


def _verdict_lines(verdict: dict) -> list[str]:
    ex, acc = verdict["pipeline_execution"], verdict["acceptance_criteria"]
    lines = [
        "## Verdict", "",
        f"* **Overall: {verdict['overall']}** (P1 gate: {verdict['p1_gate']})",
        f"* **Pipeline execution: {ex['status']}** - {ex['succeeded_all_stages']}/{ex['securities_attempted']} "
        f"securities succeeded in every stage; {len(ex['expected_unsupported'])} expected-unsupported; "
        f"{len(ex['genuine_failures'])} genuine failure(s)",
        f"* **Acceptance criteria: {acc['status']}** - {acc['counts']}",
    ]  # fmt: skip
    for reason in verdict["p1_gate_reasons"]:
        lines.append(f"  * {reason}")
    lines += ["", "### Known coverage limitations (not failures)", ""]
    lines += [f"* `{x['id']}` ({x['affects']}): {x['text']}" for x in verdict["known_coverage_limitations"]]
    return lines + [""]


def render_markdown(report: dict) -> str:
    s = report["summary"]
    lines = [f"# P0 ingestion report - run {s['run_id']} ({s['mode']})", ""]
    if s["mode"] != "LIVE":
        lines += [
            "> **SIMULATED providers.** Nothing in this report is provider data or evidence of live behaviour; it "
            "shows the pipeline mechanics only.",
            "",
        ]
    if s.get("fatal"):
        lines += [f"> **Run did not proceed: {s['fatal']}.**", ""]
    if report.get("verdict"):
        lines += _verdict_lines(report["verdict"])
    groups = s.get("outcome_groups") or {}
    lines += _table([
        ("start / end", f"{s['started_at']} / {s['ended_at']}"),
        ("runtime (s)", s["runtime_seconds"]),
        ("securities attempted", f"{s['securities_attempted']} ({s['issuers_resolved']} issuers resolved)"),
        ("succeeded all stages", f"{s['companies_succeeded_all_stages']} {groups.get('succeeded', '')}"),
        ("expected unsupported (facts only)",
         f"{s.get('companies_expected_unsupported', 0)} {groups.get('expected_unsupported', '')}"),
        ("failed", f"{s['companies_failed_any_stage']} {groups.get('failed', '')}"),
        ("identity success", _fmt(s["identity_success_rate"])),
        ("CIK mapping success", _fmt(s["cik_mapping_success"])),
        ("price success", _fmt(s["price_success"])),
        ("quote success", _fmt(s["quote_success"])),
        ("SEC fact success (excl. unsupported)", f"{_fmt(s['sec_fact_success'])} {s.get('sec_fact_outcomes', '')}"),
        ("missing core-field rate (all / industrial)",
         f"{_fmt(s['missing_core_field_rate_all'])} / {_fmt(s['missing_core_field_rate_industrial'])}"),
        ("stored duplicate keys / provider duplicate rows",
         f"{s['stored_duplicate_keys']} / {s['provider_duplicate_rows']}"),
        ("inserted / updated / unchanged", f"{s['inserted']} / {s['updated']} / {s['unchanged']}"),
        ("failures recorded", s["failed"]),
        ("requests (total, by provider)", f"{s['request_count']} {s['requests_by_provider']}"),
        ("request rate (overall /s; peak in any second)",
         f"{s['request_rate_overall_per_second']}; {s['max_requests_in_any_second']}"),
        ("HTTP 403 / 429 / retries", f"{s['http_403']} / {s['http_429']} / {s['retries']}"),
        ("provider warnings", f"{s['provider_warnings']} {s['warning_categories']}"),
        ("database size (bytes)", s["database_size_bytes"]),
    ])  # fmt: skip
    lines += [
        "",
        "## Stages",
        "",
        "| stage | status | attempted | succeeded | failed | seconds |",
        "|---|---|---|---|---|---|",
    ]
    for name, st in s["stages"].items():
        lines.append(
            f"| {name} | {st['status']} | {st['attempted']} | {st['succeeded']} | {st['failed']} | {st['seconds']} |"
        )
    lines += [
        "", "## Acceptance criteria (Phase 2 universe_pilot A1-A13)", "",
        "A criterion is PASS only if EVERY required component passed. PARTIAL = some passed, others were not "
        "evaluated; DEFERRED = data intentionally not ingested; NOT_EVALUABLE = not measurable in this run.", "",
        "| id | measurement | status | components |", "|---|---|---|---|",
    ]  # fmt: skip
    for c in report["acceptance"]:
        parts = "<br>".join(f"`{k['name']}` **{k['status']}**: {k['observed']}" for k in c["components"])
        lines.append(f"| {c['id']} | {c['measurement']} | **{c['status']}** | {parts} |")
    notes = [(c["id"], k["name"], k["note"]) for c in report["acceptance"] for k in c["components"] if k["note"]]
    infos = [(c["id"], i) for c in report["acceptance"] for i in c.get("informational", [])]
    if infos:
        lines += ["", "Reported apart (not part of a status):", ""] + [f"* {cid}: {text}" for cid, text in infos]
    if notes:
        lines += ["", "Component notes:", ""] + [f"* {cid} `{name}`: {note}" for cid, name, note in notes]
    lines += ["", "## Failures", ""]
    lines += [f"* `{i['stage']}` {i['symbol'] or '-'} **{i['kind']}**: {i['message']}" for i in report["issues"]]
    if not report["issues"]:
        lines.append("none")
    lines += ["", "## Expected unsupported coverage (not failures)", ""]
    lines += [
        f"* `{e['stage']}` {', '.join(e.get('symbols') or [e['symbol']])} CIK {e['cik']} **{e['kind']}**: "
        f"{e['message']}"
        for e in report.get("expected_unsupported", [])
    ] or ["none"]
    lines += ["", "## Provider warnings (attributed)", ""]
    if report.get("warnings"):
        lines += ["| category | ticker | provider | stage | message | details |", "|---|---|---|---|---|---|"]
        for w in report["warnings"]:
            lines.append(
                f"| {w['category']} | {w.get('symbol')} | {w.get('provider')} | {w.get('stage')} | {w['message']} | "
                f"{w.get('details')} |"
            )
    else:
        lines.append("none")
    idem = report.get("idempotency")
    if idem:
        lines += [
            "", "## Idempotency (same ingestion twice)", "",
            f"run 1: {idem['run1']}", "", f"run 2: {idem['run2']}", "", f"verdict: **{idem['verdict']}**",
        ]  # fmt: skip
    return "\n".join(lines) + "\n"
