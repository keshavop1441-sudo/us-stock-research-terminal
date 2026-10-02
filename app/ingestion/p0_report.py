"""The P0 data-quality report: measurements M1-M12 and the comparison with the Phase 2 acceptance criteria A1-A13.

Pure functions over the run reports, the validation output and the metric snapshots. Statuses are PASS / FAIL /
NOT_EVALUATED, and NOT_EVALUATED is used wherever the criterion needs a measurement that was not made (for example every
provider-behaviour criterion when the providers were simulated). Nothing is rounded up into a pass.
"""

import math
from collections import Counter
from dataclasses import dataclass

from app.ingestion.manifest import MANIFEST
from app.ingestion.run_report import RunReport
from app.screening.snapshot import CORE_LINES, IssuerSnapshot

KNOWN_WARNING_CATEGORIES = frozenset({
    "REJECTED_PRICE_ROWS", "DUPLICATE_PROVIDER_ROWS", "SPLIT_REBASE_DETECTED", "NO_NASDAQ_CLASSIFICATION",
    "NO_QUOTED_MARKET_CAP", "REJECTED_FACT_POINT", "DUPLICATE_FACT_POINTS", "TICKER_NOT_IN_SUBMISSIONS",
})  # fmt: skip
PROVIDER_ERROR_KINDS = frozenset({
    "HTTP_403", "HTTP_429", "HTTP_404", "HTTP_5XX", "NETWORK", "MALFORMED_RESPONSE", "EMPTY_DATA",
})  # fmt: skip
SEC_MAX_PER_SECOND = 9
_ROLES = {s.symbol: set(s.roles) for s in MANIFEST}
_NOT_INDUSTRIAL = {"financial", "adr"}  # reported separately from the industrial-filer thresholds (YAML A4/A5)
_ZERO_DEBT_LINES = ("short_term_debt", "current_portion_long_term_debt", "long_term_debt")
SIMULATED_NOTE = "providers were simulated: provider-behaviour criteria are not evaluated"
MECHANICAL_NOTE = "mechanical check of the write path: valid for simulated runs too"


@dataclass
class Criterion:
    id: str
    measurement: str
    threshold: str
    observed: str
    status: str  # PASS | FAIL | NOT_EVALUATED
    note: str = ""


def _pct(n: int, d: int) -> float | None:
    return None if d == 0 else n / d


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def _status(ok: bool | None) -> str:
    return "NOT_EVALUATED" if ok is None else ("PASS" if ok else "FAIL")


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


def summarise(run: RunReport, validation: dict, snapshots: dict[str, IssuerSnapshot], fy_coverage: dict) -> dict:
    """The headline measurements of ONE run (the fields the brief lists)."""
    outcomes = list(run.outcomes.values())
    attempted = len(outcomes)
    identified = sum(o.identity for o in outcomes)
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
        "companies_succeeded_all_stages": sum(o.identity and o.price and o.quote and o.facts for o in outcomes),
        "companies_failed_any_stage": sum(not (o.identity and o.price and o.quote and o.facts) for o in outcomes),
        "identity_success_rate": _pct(identified, attempted),
        "cik_mapping_success": _pct(sum(1 for o in outcomes if o.cik) - mismatched, attempted),
        "price_success": _pct(sum(o.price for o in outcomes if o.identity), identified),
        "quote_success": _pct(sum(o.quote for o in outcomes if o.identity), identified),
        "sec_fact_success": _pct(sum(o.facts for o in outcomes if o.identity), identified),
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
        "database_size_bytes": run.db_size_bytes,
        "per_security_seconds_median": sorted(seconds)[len(seconds) // 2] if seconds else None,
        "per_security_seconds_p95": p95(seconds),
        "fy_coverage": fy_coverage,
    }


def _a1_a2(run: RunReport, validation: dict, live: bool, note: str) -> list[Criterion]:
    outcomes = list(run.outcomes.values())
    n = len(outcomes)
    identified = sum(o.identity for o in outcomes)
    ambiguous = sum(1 for i in run.issues if i.kind == "IDENTITY" and "ambiguous" in i.message)
    mismatched = sum(1 for w in run.warnings if w["category"] == "TICKER_NOT_IN_SUBMISSIONS")
    unnormalised = validation["integrity"]["unnormalised_ciks"]
    return [
        Criterion(
            "A1", "M1 identity", ">= 99% resolved; 0 ambiguous",
            f"{identified}/{n} ({_fmt(_pct(identified, n))}); {ambiguous} ambiguous",
            _status((identified / n >= 0.99 and ambiguous == 0) if live else None),
            note or "with 14 securities >= 99% means all 14",
        ),
        Criterion(
            "A2", "M2 CIK mapping", "100% round-trip ticker->CIK->ticker; 0 un-normalised CIKs",
            f"{identified - mismatched}/{identified} round-trip; {unnormalised} un-normalised",
            _status((mismatched == 0 and unnormalised == 0 and identified > 0) if live else None), note,
        ),
    ]  # fmt: skip


def _a3(run: RunReport, validation: dict, live: bool, note: str) -> Criterion:
    priced = [o for o in run.outcomes.values() if o.identity]
    covered = [
        o for o in priced
        if o.price and not o.price_gaps and o.price_last and (run.as_of - o.price_last).days <= 5
    ]  # fmt: skip
    gaps = {o.symbol: [(str(a), str(b), g) for a, b, g in o.price_gaps] for o in priced if o.price_gaps}
    duplicate_keys = validation["integrity"]["duplicate_price_keys"]
    ok = (len(covered) / len(priced) >= 0.98 and duplicate_keys == 0) if live and priced else None
    return Criterion(
        "A3", "M3 prices",
        ">= 98% with full expected coverage; 0 duplicate (security_id, date); gaps > 5 trading days reviewed; "
        "split re-base detected",
        f"{len(covered)}/{len(priced)} fully covered; duplicate keys {duplicate_keys}; gaps {gaps or 'none'}; "
        f"re-bases {run.split_rebases or 'none'}",
        _status(ok),
        note or "'full coverage' = no gap over 5 business days and last bar within 5 days of the run date; "
        "exchange holidays are not removed from gap counts",
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
    ok5 = (rate is not None and rate <= 0.05 and zero_debt_unevidenced == 0 and nonfinite == 0) if live else None
    return [
        Criterion(
            "A4", "M4 SEC facts",
            ">= 95% of non-financial operating companies have revenue, net income to common, operating cash flow "
            "for >= 4 fiscal years",
            f"{len(full)}/{len(industrial)} non-financial issuers; financials and ADRs (JPM, BRK-B, TSM) apart",
            _status((len(full) / len(industrial) >= 0.95) if live and industrial else None),
            note or "total assets is not in the P0 concept set (brief section 9), so it is not part of this check",
        ),
        Criterion(
            "A5", "M5 missing data",
            "core fields missing <= 5% (industrial); 0 debt values of 0 without explicit evidence; 0 NaN/inf stored",
            f"{len(missing)}/{len(cells)} core cells missing ({_fmt(rate)}); unevidenced zero debt "
            f"{zero_debt_unevidenced}; non-finite {nonfinite}",
            _status(ok5), note or f"missing cells: {sorted(missing)}",
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
    return [
        Criterion(
            "A7", "M7 requests",
            f"SEC sustained <= {SEC_MAX_PER_SECOND}/s; requests per security recorded; full-universe projection OK",
            f"SEC peak {peak}/s over {sec_total} SEC requests ({per_issuer} per issuer incl. the one-off ticker map)",
            _status((peak is not None and peak <= SEC_MAX_PER_SECOND) if live else None),
            note or "initial-load and daily-incremental time projections need P1 measurements (P0 is 13 issuers)",
        ),
        Criterion(
            "A8", "M8 failures", "<= 1% of requests failing after retries; 0 SEC 403/429; every failure class handled",
            f"{failed} failed operations / {req_total} requests; 403={h403}; 429={h429}",
            _status((failed <= 0.01 * req_total and h403 == 0 and h429 == 0) if live and req_total else None), note,
        ),
        Criterion(
            "A9", "M9 warnings", "every provider warning categorised; 0 uncategorised",
            f"{len(run.warnings)} warnings, {len(uncategorised)} uncategorised",
            _status(len(uncategorised) == 0 if live else None),
            note or "OpenBB's own Python warnings are not intercepted: only warnings this pipeline raises are counted",
        ),
        Criterion(
            "A10", "M10 time",
            "completes without manual intervention; per-security p95 recorded; extrapolation meets A7",
            f"fatal={run.fatal}; runtime {run.runtime_seconds}s; "
            f"per-security p95 {p95([o.seconds for o in outcomes if o.seconds])}s",
            _status((run.fatal is None) if live else None),
            note or "extrapolation to the full universe is a P1/P2 question",
        ),
    ]  # fmt: skip


def _a11(first: RunReport, second: RunReport | None) -> Criterion:
    threshold = (
        "second identical run inserts 0 rows; one changed fact updates exactly 1; an amended filing adds a new "
        "accession row; reads during a write are honest"
    )
    if second is None:
        return Criterion("A11", "M11 upsert", threshold, "no second run", "NOT_EVALUATED")
    t = second.totals()
    return Criterion(
        "A11", "M11 upsert", threshold,
        f"run 2: inserted {t.inserted}, updated {t.updated}, unchanged {t.unchanged}, failed {len(second.issues)}",
        _status(t.inserted == 0 and t.updated == 0 and not second.fatal),
        "the 'changed fact', 'amendment' and 'read during write' clauses are verified by hermetic tests "
        "(tests/test_p0_pipeline.py, tests/test_upsert_accounting.py, tests/test_services.py), not by this run",
    )  # fmt: skip


def _a12_a13(snapshots, validation, live: bool, note: str, mode: str) -> list[Criterion]:
    checks = [s.cross_checks for s in snapshots.values()]
    year_high = [abs(v) for c in checks for k, v in c.items() if k.endswith(":year_high_diff")]
    cap = [abs(c["market_cap_vs_close_x_dei_shares"]) for c in checks if "market_cap_vs_close_x_dei_shares" in c]
    ok_high = (sum(v <= 0.005 for v in year_high) / len(year_high) >= 0.95) if year_high else None
    ok_cap = (sum(v <= 0.02 for v in cap) / len(cap) >= 0.90) if cap else None
    both = None if (ok_high is None or ok_cap is None or not live) else (ok_high and ok_cap)
    integrity = validation["integrity"]
    no_source = sum(integrity[k] for k in (
        "prices_without_source", "facts_without_source", "filings_without_source", "quotes_without_source",
    ))  # fmt: skip
    defects = sum(integrity[k] for k in (
        "facts_without_accession_filed_or_form", "classification_without_source",
        "sources_missing_command_or_parameters", "sources_missing_content_hash",
    ))  # fmt: skip
    return [
        Criterion(
            "A12", "M12 cross-checks",
            "52w high within 0.5% of Nasdaq year_high for >= 95%; issuer_market_cap within 2% of close x dei shares "
            "for >= 90% of single-class issuers; revenue within 1% of Nasdaq (not measured in P0)",
            f"year_high: {len(year_high)} compared; cap vs close x shares: {len(cap)} compared; Nasdaq revenue: "
            "not ingested",
            _status(both),
            note or "the revenue clause needs Nasdaq statements, which P0 does not ingest: not fully evaluable",
        ),
        Criterion(
            "A13", "M5 provenance",
            "100% of provider rows carry source_id; 100% of SEC fact rows carry accession_no, filed_date, form; "
            "classification stored with source",
            f"rows without source_id: {no_source}; provenance defects: {defects}",
            _status(no_source == 0 and defects == 0), MECHANICAL_NOTE if mode != "LIVE" else "",
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
        f"after run 1: {dup1}; after run 2: {'not run' if dup2 is None else dup2}",
        "FAIL" if (dup1 or dup2) else ("NOT_EVALUATED" if second_validation is None else "PASS"),
        MECHANICAL_NOTE if first.mode != "LIVE" else "",
    )  # fmt: skip
    return [
        *_a1_a2(first, first_validation, live, note),
        _a3(first, first_validation, live, note),
        *_a4_a5(first, first_validation, snapshots, fy_coverage, live, note),
        a6,
        *_a7_a10(first, live, note),
        _a11(first, second),
        *_a12_a13(snapshots, first_validation, live, note, first.mode),
    ]


def _table(rows: list[tuple[str, object]]) -> list[str]:
    return ["| measurement | value |", "|---|---|", *(f"| {k} | {v} |" for k, v in rows)]


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
    lines += _table([
        ("start / end", f"{s['started_at']} / {s['ended_at']}"),
        ("runtime (s)", s["runtime_seconds"]),
        ("securities attempted", f"{s['securities_attempted']} ({s['issuers_resolved']} issuers resolved)"),
        ("succeeded all stages / failed any stage",
         f"{s['companies_succeeded_all_stages']} / {s['companies_failed_any_stage']}"),
        ("identity success", _fmt(s["identity_success_rate"])),
        ("CIK mapping success", _fmt(s["cik_mapping_success"])),
        ("price success", _fmt(s["price_success"])),
        ("quote success", _fmt(s["quote_success"])),
        ("SEC fact success", _fmt(s["sec_fact_success"])),
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
        "| id | measurement | threshold | observed | status | note |", "|---|---|---|---|---|---|",
    ]  # fmt: skip
    for c in report["acceptance"]:
        lines.append(
            f"| {c['id']} | {c['measurement']} | {c['threshold']} | {c['observed']} | **{c['status']}** | {c['note']} |"
        )
    lines += ["", "## Failures", ""]
    lines += [f"* `{i['stage']}` {i['symbol'] or '-'} **{i['kind']}**: {i['message']}" for i in report["issues"]]
    if not report["issues"]:
        lines.append("none")
    idem = report.get("idempotency")
    if idem:
        lines += [
            "", "## Idempotency (same ingestion twice)", "",
            f"run 1: {idem['run1']}", "", f"run 2: {idem['run2']}", "", f"verdict: **{idem['verdict']}**",
        ]  # fmt: skip
    return "\n".join(lines) + "\n"
