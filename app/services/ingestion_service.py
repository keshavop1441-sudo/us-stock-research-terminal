"""P0 ingestion: SOURCE -> FETCH -> RAW PROVENANCE -> NORMALISATION -> VALIDATION -> UPSERT (derived metrics:
``metrics_service``).

This is the only place that opens the write repository for provider data. Fetching (``app.ingestion.*_source``) and
normalisation (``app.ingestion.*_normalize``) are separate pure-ish modules; calculations live in ``app.screening``
and are not stored (raw facts only).

Concurrency (CLAUDE.md "single writer"): the whole run happens inside ONE ``access.writer(...)`` block, i.e. the refresh
process holds the writer lock and one transaction for its whole run. Consequences, by design:
* if another writer is active nothing is fetched and the run reports ``DATABASE_LOCKED`` (no partial work);
* a failure while FETCHING or NORMALISING one symbol is caught, recorded in ``report.issues`` and that symbol is
  skipped; the other symbols are still written;
* a DATABASE error (a bug) aborts the run and rolls EVERYTHING back (``DATABASE_ERROR``): there is no half-written
  state. Readers see "unavailable" for the duration, as for any refresh (``app.services.db``).

Stage order: identity -> prices -> quotes -> sec_facts. A symbol without an identity is not fetched further.
"""

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

import duckdb
from pydantic import ValidationError

from app.database import access
from app.database.errors import DatabaseUnavailableError
from app.ingestion import market_normalize as mn
from app.ingestion import sec_normalize as sn
from app.ingestion.errors import IngestionError, MissingUserAgentError, NoFactsError, UnsupportedTaxonomyError
from app.ingestion.manifest import MANIFEST, PRICE_HISTORY_START, P0Security, manifest_digest, primary_symbol_for
from app.ingestion.market_source import MarketData
from app.ingestion.raw_store import RawStore
from app.ingestion.retrieval import Retrieval, utc_now
from app.ingestion.run_report import RunReport, StageResult, SymbolOutcome
from app.ingestion.sec_http import SecHttpClient
from app.ingestion.sec_source import fetch_companyfacts, fetch_submissions, fetch_ticker_map
from app.ingestion.stats import RequestStats
from app.models.records import SecurityRecord
from app.models.symbols import canonical_symbol

STAGES = ("identity", "prices", "quotes", "sec_facts")
# Named tuple, not `except A, B:` (3.14-only syntax; see app/database/locking.py).
_RECORD_ERRORS = (ValidationError, ValueError)


class P0Run:
    """One execution. ``sec`` is None when SEC_USER_AGENT is missing: the run then reports it cannot proceed."""

    def __init__(
        self,
        db_path: Path,
        *,
        run_id: str,
        mode: str,
        as_of: date,
        sec: SecHttpClient | None,
        market: MarketData,
        stats: RequestStats,
        raw_dir: Path | None = None,
        manifest: tuple[P0Security, ...] = MANIFEST,
        price_start: date = PRICE_HISTORY_START,
        clock: Callable[[], datetime] = utc_now,
        stages: tuple[str, ...] = STAGES,
    ):
        unknown = set(stages) - set(STAGES)
        if unknown or "identity" not in stages:
            raise ValueError(f"stages must include 'identity' and be a subset of {STAGES}; got {stages}")
        self.run_stages = stages
        self.db_path, self.sec, self.market, self.stats = db_path, sec, market, stats
        self.manifest, self.price_start, self.clock, self.as_of = manifest, price_start, clock, as_of
        self.raw = RawStore(raw_dir, run_id) if raw_dir else None
        self.report = RunReport(
            run_id=run_id,
            mode=mode,
            manifest_digest=manifest_digest(manifest),
            symbols=[s.symbol for s in manifest],
            as_of=as_of,
            stages={name: StageResult(name) for name in STAGES},
            outcomes={s.symbol: SymbolOutcome(s.symbol) for s in manifest},
        )
        self._security_ids: dict[str, int] = {}
        self._issuers: dict[str, list[str]] = {}  # cik -> manifest symbols

    # --- orchestration --------------------------------------------------------------------------------------------

    def run(self) -> RunReport:
        report = self.report
        report.started_at = self.clock()
        if self.sec is None:
            report.fatal = MissingUserAgentError.kind
            report.issue(
                "run", None, MissingUserAgentError.kind, "SEC_USER_AGENT is not set: live SEC ingestion cannot proceed."
            )
            for stage in report.stages.values():
                stage.status = "SKIPPED"
        else:
            try:
                with access.writer(self.db_path, "P0 ingestion") as w:
                    self._identity(w)
                    if "prices" in self.run_stages:
                        self._prices(w)
                    if "quotes" in self.run_stages:
                        self._quotes(w)
                    if "sec_facts" in self.run_stages:
                        self._facts(w)
            except DatabaseUnavailableError as exc:
                report.fatal = "DATABASE_LOCKED"
                report.issue("run", None, "DATABASE_LOCKED", str(exc))
                for stage in report.stages.values():
                    stage.status = "SKIPPED"
            except duckdb.Error as exc:
                report.fatal = "DATABASE_ERROR"
                report.issue(
                    "run", None, "DATABASE_ERROR", f"{type(exc).__name__}: {exc} (the whole run was rolled back)"
                )
        report.ended_at = self.clock()
        report.requests = self.stats.snapshot()
        report.db_size_bytes = self.db_path.stat().st_size if self.db_path.is_file() else None
        return report

    # --- helpers --------------------------------------------------------------------------------------------------

    def _stage(self, name: str) -> StageResult:
        stage = self.report.stages[name]
        stage.status, stage.started_at = "OK", self.clock()
        return stage

    def _finish(self, stage: StageResult) -> None:
        stage.ended_at = self.clock()
        stage.status = "OK" if stage.failed == 0 else ("FAILED" if stage.succeeded == 0 else "PARTIAL")

    def _save_raw(self, retrieval: Retrieval, label: str) -> None:
        if self.raw and not retrieval.raw_path:
            self.raw.save(retrieval, label)

    def _drain_openbb_warnings(self, stage: str) -> None:
        """Python warnings OpenBB raised during the last call(s), attributed to provider/command/symbol."""
        for item in self.market.drain_warnings():
            self.report.warn(
                "OPENBB_WARNING", item["symbol"], item["message"], provider=item["provider"], stage=stage,
                command=item["command"], sent_symbol=item["sent_symbol"], warning_class=item["category"],
            )  # fmt: skip

    def _record(self, w, retrieval: Retrieval, label: str) -> int:
        self._save_raw(retrieval, label)
        self.report.retrievals_recorded += 1
        self.report.fallback_retrievals += int(retrieval.is_fallback)
        return w.record_source(retrieval.to_source_record())

    def _fail(self, stage: StageResult, name: str, symbol: str | None, error: Exception) -> None:
        stage.failed += 1
        kind = getattr(error, "kind", None) or ("INVALID_RECORD" if isinstance(error, _RECORD_ERRORS) else "ERROR")
        self.report.issue(name, symbol, kind, str(error))

    def _active(self, require: str) -> list[P0Security]:
        """Manifest securities that passed the previous stage (``require`` names the SymbolOutcome flag)."""
        return [s for s in self.manifest if getattr(self.report.outcomes[s.symbol], require)]

    # --- stage 1: identity ----------------------------------------------------------------------------------------

    def _identity(self, w) -> None:
        stage, report = self._stage("identity"), self.report
        stage.attempted = len(self.manifest)
        try:
            retrieval = fetch_ticker_map(self.sec)
            self._record(w, retrieval, "all")
            ticker_map = sn.parse_ticker_map(retrieval.payload)  # type: ignore[arg-type]
        except IngestionError as exc:
            for sec in self.manifest:
                self._fail(stage, "identity", sec.symbol, exc)
            self._finish(stage)
            return
        profiles: dict[str, tuple[sn.IssuerProfile, int]] = {}
        for sec in self.manifest:
            outcome = report.outcomes[sec.symbol]
            try:
                entry = sn.resolve(ticker_map, sec.symbol, sec.phase2_cik)
                if entry.cik not in profiles:
                    sub = fetch_submissions(self.sec, entry.cik)
                    sub_id = self._record(w, sub, f"CIK{entry.cik}")
                    profile, filings = sn.parse_submissions(sub.payload, entry.cik)  # type: ignore[arg-type]
                    profiles[entry.cik] = (profile, sub_id)
                    rows = [f.model_copy(update={"source_id": sub_id}) for f in filings]
                    report.counts("filings").add(w.upsert_filings(rows))
                profile, _ = profiles[entry.cik]
                if canonical_symbol(sec.symbol) not in profile.tickers:
                    report.warn("TICKER_NOT_IN_SUBMISSIONS", sec.symbol, f"submissions list {list(profile.tickers)}")
                self._security_ids[sec.symbol] = w.upsert_security(
                    SecurityRecord(
                        ticker=entry.ticker,
                        cik=entry.cik,
                        name=entry.name or profile.name,
                        exchange=entry.exchange,
                        sic=profile.sic,
                        sic_source="sec" if profile.sic else None,
                        is_active=True,
                    )
                )
                outcome.cik, outcome.identity = entry.cik, True
                self._issuers.setdefault(entry.cik, []).append(sec.symbol)
                stage.succeeded += 1
            except (IngestionError, *_RECORD_ERRORS) as exc:
                self._fail(stage, "identity", sec.symbol, exc)
        report.multi_class = {cik: symbols for cik, symbols in self._issuers.items() if len(symbols) > 1}
        self._finish(stage)

    # --- stage 2: prices ------------------------------------------------------------------------------------------

    def _prices(self, w) -> None:
        stage, report = self._stage("prices"), self.report
        todo = self._active("identity")
        stage.attempted = len(todo)
        for sec in todo:
            outcome, started = report.outcomes[sec.symbol], self.clock()
            try:
                try:
                    retrieval = self.market.historical(sec.symbol, self.price_start, self.as_of)
                finally:
                    self._drain_openbb_warnings("prices")
                source_id = self._record(w, retrieval, sec.symbol)
                parsed = mn.parse_prices(self._security_ids[sec.symbol], retrieval.payload, source_id)  # type: ignore[arg-type]
                where = {
                    "provider": retrieval.provider, "stage": "prices", "command": retrieval.command,
                    "sent_symbol": retrieval.parameters.get("symbol"), "is_fallback": retrieval.is_fallback,
                }  # fmt: skip
                for reason, n in mn.count_rejects(parsed.rejects).items():
                    rows = [r["row"] for r in parsed.rejects if r["reason"] == reason]
                    report.warn(
                        "REJECTED_PRICE_ROWS", sec.symbol, f"{n} row(s) rejected: {reason}",
                        reason=reason, rows=rows[:5], **where,
                    )  # fmt: skip
                if parsed.duplicates:
                    conflicting = [d for d in parsed.duplicate_details if not d["identical"]]
                    report.warn(
                        "CONFLICTING_DUPLICATE_PROVIDER_ROWS" if conflicting else "DUPLICATE_PROVIDER_ROWS",
                        sec.symbol,
                        f"{parsed.duplicates} repeated trade date(s), last kept"
                        + (f"; {len(conflicting)} with DIFFERENT values" if conflicting else " (all identical)"),
                        duplicates=parsed.duplicates, conflicting=len(conflicting),
                        dates=[d["date"] for d in parsed.duplicate_details[:10]],
                        samples=parsed.duplicate_details[:5], **where,
                    )  # fmt: skip
                if not parsed.records:
                    raise IngestionError(f"{sec.symbol}: no usable price rows")
                ratio = mn.detect_rebase(w.price_closes(self._security_ids[sec.symbol]), parsed.records)
                if ratio is not None:
                    report.split_rebases[sec.symbol] = ratio
                    report.warn(
                        "SPLIT_REBASE_DETECTED", sec.symbol, f"overlapping closes changed by x{ratio:.4f}",
                        ratio=ratio, **where,
                    )  # fmt: skip
                report.counts("price_daily").add(w.upsert_prices(parsed.records))
                outcome.price, outcome.price_rows = True, len(parsed.records)
                outcome.price_first, outcome.price_last, outcome.price_gaps = parsed.first, parsed.last, parsed.gaps
                outcome.used_fallback_prices = retrieval.is_fallback
                stage.succeeded += 1
            except (IngestionError, *_RECORD_ERRORS) as exc:
                self._fail(stage, "prices", sec.symbol, exc)
            outcome.seconds += (self.clock() - started).total_seconds()
        self._finish(stage)

    # --- stage 3: quotes (market cap, provider 52-week range, raw Nasdaq classification) ------------------------------

    def _quotes(self, w) -> None:
        stage, report = self._stage("quotes"), self.report
        todo = self._active("identity")
        stage.attempted = len(todo)
        for sec in todo:
            outcome, started = report.outcomes[sec.symbol], self.clock()
            try:
                try:
                    retrieval = self.market.quote(sec.symbol)
                finally:
                    self._drain_openbb_warnings("quotes")
                source_id = self._record(w, retrieval, sec.symbol)
                raw_quote = retrieval.payload[0]  # type: ignore[index]
                where = {"provider": retrieval.provider, "stage": "quotes", "command": retrieval.command,
                         "sent_symbol": retrieval.parameters.get("symbol")}  # fmt: skip
                parsed = mn.parse_quote(self._security_ids[sec.symbol], retrieval.payload[0], source_id)  # type: ignore[index]
                report.counts("market_quotes").add(w.upsert_market_quotes([parsed.record]))
                if parsed.sector or parsed.industry:  # raw Nasdaq strings, stored WITH their source system
                    w.upsert_security(
                        SecurityRecord(
                            ticker=canonical_symbol(sec.symbol),
                            cik=outcome.cik,
                            sector=parsed.sector,
                            industry=parsed.industry,
                            sector_source="nasdaq",
                        )
                    )
                else:
                    report.warn(
                        "NO_NASDAQ_CLASSIFICATION", sec.symbol, "quote carried no usable sector/industry",
                        raw_sector=raw_quote.get("sector"), raw_industry=raw_quote.get("industry"),
                        raw_exchange=raw_quote.get("exchange"), **where,
                    )  # fmt: skip
                if parsed.record.market_cap is None:
                    report.warn(
                        "NO_QUOTED_MARKET_CAP", sec.symbol, "issuer_market_cap will be MISSING_INPUT",
                        raw_market_cap=raw_quote.get("market_cap"), **where,
                    )  # fmt: skip
                outcome.quote = True
                stage.succeeded += 1
            except (IngestionError, *_RECORD_ERRORS) as exc:
                self._fail(stage, "quotes", sec.symbol, exc)
            outcome.seconds += (self.clock() - started).total_seconds()
        self._finish(stage)

    # --- stage 4: raw SEC accounting facts ----------------------------------------------------------------------------

    def _facts(self, w) -> None:
        stage, report = self._stage("sec_facts"), self.report
        stage.attempted = len(self._issuers)
        for cik, symbols in self._issuers.items():
            started = self.clock()
            try:
                retrieval = fetch_companyfacts(self.sec, cik)
                self._save_raw(retrieval, f"CIK{cik}")
                parsed = sn.parse_companyfacts(retrieval.payload, cik)  # type: ignore[arg-type]
                where = {"provider": "sec", "stage": "sec_facts", "command": retrieval.command, "cik": cik}
                if parsed.unsupported_taxonomy:
                    # EXPECTED coverage gap (IFRS issuer): recorded on the retrieval so later reads know WHY there are
                    # no facts, reported as expected_unsupported, never as a failure and never as zero.
                    retrieval.notes.append(f"UNSUPPORTED_TAXONOMY:{parsed.unsupported_taxonomy}")
                    self._record(w, retrieval, f"CIK{cik}")
                    stage.expected_unsupported += 1
                    report.expected_unsupported.append(
                        {
                            "stage": "sec_facts", "symbol": symbols[0], "symbols": list(symbols), "cik": cik,
                            "kind": UnsupportedTaxonomyError.kind, "taxonomy": parsed.unsupported_taxonomy,
                            "taxonomies": list(parsed.taxonomies),
                            "message": f"companyfacts uses '{parsed.unsupported_taxonomy}' only "
                            f"(taxonomies {list(parsed.taxonomies)}): fundamentals unavailable",
                        }
                    )  # fmt: skip
                    for symbol in symbols:
                        report.outcomes[symbol].facts_status = UnsupportedTaxonomyError.kind
                    continue
                source_id = self._record(w, retrieval, f"CIK{cik}")
                for reject in parsed.rejects:
                    report.warn("REJECTED_FACT_POINT", symbols[0], f"{reject['concept']}: {reject['error']}", **where)
                for reason, n in parsed.skipped.items():
                    report.facts_skipped[reason] = report.facts_skipped.get(reason, 0) + n
                if parsed.duplicates:
                    report.warn(
                        "DUPLICATE_FACT_POINTS", symbols[0],
                        f"{parsed.duplicates} repeated logical keys in the document", duplicates=parsed.duplicates,
                        **where,
                    )  # fmt: skip
                if not parsed.points:
                    raise NoFactsError(
                        f"CIK {cik}: companyfacts holds no allow-listed us-gaap/dei facts "
                        f"(taxonomies: {list(parsed.taxonomies)})"
                    )
                records = [p.to_record(source_id) for p in parsed.points]
                report.counts("financial_facts").add(w.upsert_financial_facts(records))
                for symbol in symbols:
                    outcome = report.outcomes[symbol]
                    outcome.facts, outcome.fact_rows, outcome.facts_status = True, len(records), "OK"
                stage.succeeded += 1
            except (IngestionError, *_RECORD_ERRORS) as exc:
                self._fail(stage, "sec_facts", symbols[0], exc)
                for symbol in symbols:
                    report.outcomes[symbol].facts_status = "FAILED"
            finally:
                for symbol in symbols:
                    report.outcomes[symbol].seconds += (self.clock() - started).total_seconds()
        self._finish(stage)


def primary_symbols() -> dict[str, str]:
    return {s.symbol: primary_symbol_for(s.symbol) for s in MANIFEST}


# --- the P0 sequence: ingest, validate, ingest the SAME thing again, validate idempotency, report -------------------


def _snapshot_dict(snapshots) -> dict[str, object]:
    def metric(r) -> dict[str, object]:
        return {"state": r.state.value, "value": r.value, "reason": r.reason, "flags": list(r.flags)}

    return {
        cik: {
            "as_of": snap.as_of,
            "issuer_metrics": {k: metric(v) for k, v in snap.issuer.items()},
            "listing_metrics": {sym: {k: metric(v) for k, v in ms.items()} for sym, ms in snap.listings.items()},
            "lines": snap.lines,
            "cross_checks": snap.cross_checks,
            "coverage": snap.coverage,
        }
        for cik, snap in snapshots.items()
    }


def run_p0_sequence(
    db_path: Path, make_run: Callable[[str], P0Run], *, second_run: bool = True, automated_tests: str | None = None
) -> dict[str, object]:
    """Run P0 once, validate, optionally run the identical ingestion again and validate idempotency; return the report.

    ``make_run(run_id)`` builds a fresh ``P0Run`` (fresh clients and request counters) for each execution, so a re-run
    is exactly the same configuration, not a continuation. ``automated_tests`` ("PASSED" / "FAILED" / None = not
    reported) is an input of the P1 progression gate, which this function cannot measure itself.
    """
    from app.ingestion import p0_report as pr
    from app.services import p0_metrics_service as ms

    first = make_run("run1").run()
    report: dict[str, object] = {"first_run": first.to_dict()}
    if first.fatal:
        validation1: dict = {"integrity": {}, "table_counts": {}, "sources": [], "securities": []}
        snapshots1: dict = {}
        coverage1: dict = {}
        report["summary"] = {**_empty_summary(first), "fatal": first.fatal}
        criteria = pr.not_run_criteria(f"the run did not proceed ({first.fatal})")
        report["acceptance"] = [c.to_dict() for c in criteria]
        report["verdict"] = pr.verdict(first, criteria, report["summary"], automated_tests=automated_tests)  # type: ignore[arg-type]
        report["issues"] = [i.__dict__ for i in first.issues]
        report["warnings"] = first.warnings
        return report
    validation1 = ms.validation(db_path)
    snapshots1 = ms.compute_snapshots(db_path, first.as_of)
    coverage1 = ms.fiscal_year_coverage(db_path, sorted(snapshots1))
    second = validation2 = None
    if second_run:
        second = make_run("run2").run()
        validation2 = ms.validation(db_path) if not second.fatal else None
        report["second_run"] = second.to_dict()
    report["summary"] = pr.summarise(first, validation1, snapshots1, coverage1)
    criteria = pr.evaluate(first, validation1, snapshots1, coverage1, second=second, second_validation=validation2)
    report["acceptance"] = [c.to_dict() for c in criteria]
    report["verdict"] = pr.verdict(  # type: ignore[arg-type]
        first, criteria, report["summary"], second, integrity=validation1["integrity"], automated_tests=automated_tests
    )
    report["issues"] = [i.__dict__ for i in first.issues]
    report["warnings"] = first.warnings
    report["expected_unsupported"] = first.expected_unsupported
    report["validation_after_run1"] = validation1
    report["metrics"] = _snapshot_dict(snapshots1)
    report["manifest"] = [s.__dict__ for s in MANIFEST]
    if second is not None:
        t1, t2 = first.totals(), second.totals()
        zero = t2.inserted == 0 and t2.updated == 0 and not second.fatal
        report["idempotency"] = {
            "run1": {
                "inserted": t1.inserted,
                "updated": t1.updated,
                "unchanged": t1.unchanged,
                "issues": len(first.issues),
            },
            "run2": {
                "inserted": t2.inserted,
                "updated": t2.updated,
                "unchanged": t2.unchanged,
                "issues": len(second.issues),
            },
            "per_table_run2": {t: c.__dict__ for t, c in second.tables.items()},
            "table_counts_after_run1": validation1["table_counts"],
            "table_counts_after_run2": validation2["table_counts"] if validation2 else None,
            "retrieval_rows_appended_run2": second.retrievals_recorded,
            "expected_unsupported_run1": [e["symbol"] for e in first.expected_unsupported],
            "expected_unsupported_run2": [e["symbol"] for e in second.expected_unsupported],
            "verdict": "PASS: no logical record was inserted or changed" if zero else "FAIL",
        }
        report["second_run_summary"] = pr.summarise(second, validation2 or validation1, snapshots1, coverage1)
    return report


def _empty_summary(run: RunReport) -> dict[str, object]:
    """Summary of a run that never started: every measurement is absent (None / 0), nothing is estimated."""
    unmeasured = (
        "identity_success_rate", "cik_mapping_success", "price_success", "quote_success", "sec_fact_success",
        "missing_core_field_rate_all", "missing_core_field_rate_industrial", "request_rate_overall_per_second",
    )  # fmt: skip
    zero = (
        "issuers_resolved", "companies_succeeded_all_stages", "stored_duplicate_keys", "provider_duplicate_rows",
        "inserted", "updated", "unchanged", "http_403", "http_429", "retries", "provider_warnings",
    )  # fmt: skip
    stages = {
        name: {"status": s.status, "attempted": 0, "succeeded": 0, "failed": 0, "seconds": None}
        for name, s in run.stages.items()
    }
    return {
        "run_id": run.run_id, "mode": run.mode, "started_at": run.started_at, "ended_at": run.ended_at,
        "runtime_seconds": run.runtime_seconds, "stages": stages, "securities_attempted": len(run.symbols),
        "companies_failed_any_stage": len(run.symbols), "failed": len(run.issues),
        "request_count": run.requests.get("requests_total", 0), "requests_by_provider": {},
        "max_requests_in_any_second": {}, "warning_categories": {}, "database_size_bytes": run.db_size_bytes,
        **dict.fromkeys(unmeasured), **dict.fromkeys(zero, 0),
    }  # fmt: skip
