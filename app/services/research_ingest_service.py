"""Ingest named symbols into the local database through the audited P0 pipeline, and report compactly.

This is the SAME fetch -> raw provenance -> normalise -> validate -> upsert path the P0 pilot uses (``P0Run``): SEC
requests go through ``SecHttpClient`` (identifying User-Agent, rate ceiling, 403 never retried), prices/quotes through
OpenBB (Cboe primary, Nasdaq fallback). Nothing here fetches data any other way and nothing is stored but raw facts.
The run is idempotent: re-ingesting the same symbols reports ``unchanged`` rows and appends only provenance rows.
"""

from datetime import date
from pathlib import Path
from typing import Any

from app.database.connection import ensure_database
from app.ingestion.manifest import PRICE_HISTORY_START
from app.ingestion.market_source import MarketData
from app.ingestion.sec_http import SecHttpClient
from app.ingestion.stats import RequestStats
from app.research.universe import DEFAULT_STAGES, build_manifest
from app.services.ingestion_service import P0Run


def ingest_symbols(
    db_path: Path,
    symbols: list[str],
    *,
    as_of: date,
    sec: SecHttpClient | None,
    market: MarketData,
    stats: RequestStats,
    stages: tuple[str, ...] = DEFAULT_STAGES,
    price_start: date = PRICE_HISTORY_START,
    run_id: str = "research",
    raw_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the pipeline for ``symbols`` and return a plain-data summary (no acceptance verdict: this is not P0)."""
    manifest = build_manifest(symbols)
    ensure_database(db_path)
    run = P0Run(
        db_path,
        run_id=run_id,
        mode="LIVE",
        as_of=as_of,
        sec=sec,
        market=market,
        stats=stats,
        raw_dir=raw_dir,
        manifest=manifest,
        price_start=price_start,
        stages=stages,
    )
    report = run.run()
    per_symbol = {}
    for symbol, outcome in report.outcomes.items():
        per_symbol[symbol] = {
            "cik": outcome.cik,
            "identity": outcome.identity,
            "prices": {
                "loaded": outcome.price,
                "rows": outcome.price_rows,
                "first": outcome.price_first,
                "last": outcome.price_last,
                "fallback_provider_used": outcome.used_fallback_prices,
            },
            "quote": outcome.quote,
            "sec_facts": {"status": outcome.facts_status, "rows": outcome.fact_rows},
        }
    return {
        "as_of": report.as_of,
        "stages_requested": list(stages),
        "stage_status": {name: stage.status for name, stage in report.stages.items()},
        "fatal": report.fatal,
        "symbols": per_symbol,
        "tables": {t: c.__dict__ for t, c in report.tables.items()},
        "multi_class_issuers": report.multi_class,
        "expected_unsupported": report.expected_unsupported,
        "issues": [i.__dict__ for i in report.issues],
        "warnings": report.warnings[:50],
        "warnings_total": len(report.warnings),
        "requests": report.requests,
        "provenance_rows_appended": report.retrievals_recorded,
        "runtime_seconds": report.runtime_seconds,
    }
