"""Read-side research operations: universe overview, canonical metrics, deterministic screens.

Everything is computed AT READ TIME from the stored raw facts and prices by ``app.screening`` (nothing derived is ever
stored). Outputs are plain data (JSON-ready); every metric is a ``{state, value, reason, flags}`` record, and the
filing / retrieval each value came from is attached as provenance.
"""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.database import access
from app.models.symbols import canonical_symbol
from app.research import freshness as fr
from app.research.catalog import CATALOG
from app.research.envelope import metric_dict
from app.research.screen_spec import ScreenSpec
from app.research.screening import SecurityView, run_screen
from app.screening import classification as cls
from app.screening.snapshot import IssuerSnapshot
from app.services import p0_metrics_service as ms

_PRICE_METRICS = {name for name, spec in CATALOG.items() if spec.scope == "listing"}


@dataclass
class LoadedSecurity:
    view: SecurityView
    snapshot: IssuerSnapshot
    security_id: int
    freshness: dict[str, Any]
    sources: dict[str, Any]
    last_close: float | None = None
    listing_row: dict[str, Any] = field(default_factory=dict)


@dataclass
class LoadedUniverse:
    securities: list[LoadedSecurity]
    as_of: date
    not_in_database: list[str] = field(default_factory=list)
    skipped_without_cik: list[str] = field(default_factory=list)


def _line_refs(snap: IssuerSnapshot, lines: tuple[str, ...]) -> list[dict[str, Any]]:
    refs = []
    for line in lines:
        rec = snap.lines.get(line)
        if not rec:
            continue
        refs.append({"kind": "sec_xbrl_line", "line": line, **{k: v for k, v in rec.items() if k != "flags"}})
    return refs


def _provenance(snap: IssuerSnapshot, sources: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for name, spec in CATALOG.items():
        refs = _line_refs(snap, spec.lines)
        if spec.uses_prices:
            for key in ("price", "quote"):
                if sources.get(key):
                    refs.append({"kind": f"{key}_retrieval", **sources[key]})
        out[name] = refs
    return out


def _source_ref(row: dict | None) -> dict[str, Any] | None:
    if not row:
        return None
    keys = (
        "source_id",
        "provider",
        "dataset",
        "command",
        "url",
        "retrieved_at",
        "as_of",
        "content_hash",
        "is_fallback",
        "provider_version",
    )
    return {k: row.get(k) for k in keys}


def load_universe(
    db_path: Path,
    as_of: date,
    symbols: list[str] | None = None,
    *,
    max_price_age_days: int | None = fr.DEFAULT_MAX_PRICE_AGE_DAYS,
) -> LoadedUniverse:
    """Snapshots + provenance + freshness for the requested stored symbols (default: everything stored)."""
    with access.reader(db_path) as r:
        rows = r.securities_of_issuers().to_dicts()
    stored = {row["ticker"] for row in rows}
    wanted = {canonical_symbol(s) for s in symbols} if symbols is not None else stored
    result = LoadedUniverse([], as_of, not_in_database=sorted(wanted - stored))
    result.skipped_without_cik = sorted(row["ticker"] for row in rows if row["ticker"] in wanted and not row["cik"])
    snapshots = ms.compute_snapshots(db_path, as_of, symbols=wanted & stored)
    with access.reader(db_path) as r:
        extras: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["ticker"] not in wanted or not row["cik"]:
                continue
            quotes = r.market_quotes(row["security_id"])
            bar = r.last_price_bar(row["security_id"])
            extras[row["ticker"]] = {
                "last_bar": bar[0] if bar else None,
                "last_close": bar[1] if bar else None,
                "quote_date": quotes["quote_date"][0] if quotes.height else None,
                "price_source": _source_ref(r.price_source(row["security_id"])),
                "quote_source": _source_ref(r.quote_source(row["security_id"])),
                "facts_source": _source_ref(r.facts_source(row["cik"])),
            }
        for row in rows:
            ticker = row["ticker"]
            if ticker not in extras or row["cik"] not in snapshots:
                continue
            snap, extra = snapshots[row["cik"]], extras[ticker]
            primary = snap.cross_checks.get("primary_listing", ticker)
            primary_quote_date = extras.get(primary, extra)["quote_date"]
            issuer = fr.apply_issuer_freshness(snap.issuer, primary_quote_date, as_of, max_price_age_days)
            listing = fr.apply_listing_freshness(
                snap.listings.get(ticker, {}), _PRICE_METRICS, extra["last_bar"], as_of, max_price_age_days
            )
            sources = {
                "price": extra["price_source"],
                "quote": extras.get(primary, extra)["quote_source"],
                "facts": extra["facts_source"],
            }
            nasdaq = (
                cls.nasdaq_classification(row["sector"], row["industry"]) if row["sector_source"] == "nasdaq" else None
            )
            sic = cls.sic_classification(row["sic"], None) if row["sic_source"] == "sec" and row["sic"] else None
            view = SecurityView(
                ticker=ticker,
                cik=row["cik"],
                name=row["name"],
                issuer=issuer,
                listing=listing,
                nasdaq=nasdaq,
                sic=sic,
                provenance=_provenance(snap, sources),
            )
            result.securities.append(
                LoadedSecurity(
                    view,
                    snap,
                    row["security_id"],
                    fr.freshness_record(extra["last_bar"], primary_quote_date, as_of, max_price_age_days),
                    sources,
                    extra["last_close"],
                    row,
                )
            )
    result.securities.sort(key=lambda s: (s.view.ticker, s.view.cik))
    return result


def stored_tickers(db_path: Path) -> set[str]:
    """Tickers currently stored (empty when there is no database yet)."""
    if not db_path.is_file():
        return set()
    with access.reader(db_path) as r:
        return set(r.securities_of_issuers()["ticker"].to_list())


# --- universe overview ----


def universe_overview(db_path: Path, as_of: date) -> dict[str, Any]:
    """What is in the local database: one row per stored listing, with coverage and data dates (no metrics)."""
    with access.reader(db_path) as r:
        rows = r.securities_of_issuers().to_dicts()
        counts = r.table_counts()
        version = r.schema_version()
        last_sync = r.last_sync()
        listings = []
        for row in rows:
            quotes = r.market_quotes(row["security_id"])
            notes = r.fact_support_notes(row["cik"]) if row["cik"] else []
            listings.append(
                {
                    "ticker": row["ticker"],
                    "cik": row["cik"],
                    "name": row["name"],
                    "exchange": row["exchange"],
                    "nasdaq_sector": row["sector"] if row["sector_source"] == "nasdaq" else None,
                    "nasdaq_industry": row["industry"] if row["sector_source"] == "nasdaq" else None,
                    "sec_sic": row["sic"] if row["sic_source"] == "sec" else None,
                    "last_price_bar": (r.last_price_bar(row["security_id"]) or (None,))[0],
                    "last_quote_date": quotes["quote_date"][0] if quotes.height else None,
                    "fact_rows": len(r.financial_facts(row["cik"])) if row["cik"] else 0,
                    "fundamentals_unavailable_reason": next(
                        (n for n in notes if n.startswith("UNSUPPORTED_TAXONOMY:")), None
                    ),
                }
            )
    return {
        "as_of": as_of,
        "schema_version": version,
        "table_counts": counts,
        "last_retrieval_at": last_sync,
        "securities": listings,
        "scope_note": "Screens run on these stored securities only; the full market universe is not loaded.",
    }


# --- canonical metrics ----


def security_metrics(item: LoadedSecurity) -> dict[str, Any]:
    snap = item.snapshot
    return {
        "ticker": item.view.ticker,
        "cik": item.view.cik,
        "name": item.view.name,
        "as_of": snap.as_of,
        "coverage": snap.coverage,
        "freshness": item.freshness,
        "issuer_metrics": {k: metric_dict(v) for k, v in item.view.issuer.items()},
        "listing_metrics": {k: metric_dict(v) for k, v in item.view.listing.items()},
        "lines": snap.lines,
        "cross_checks": snap.cross_checks,
        "retrievals": item.sources,
    }


def metrics_for(db_path: Path, symbols: list[str], as_of: date, *, max_price_age_days: int | None) -> dict[str, Any]:
    loaded = load_universe(db_path, as_of, symbols, max_price_age_days=max_price_age_days)
    return {
        "as_of": as_of,
        "securities": [security_metrics(s) for s in loaded.securities],
        "not_in_database": loaded.not_in_database,
        "skipped_without_cik": loaded.skipped_without_cik,
    }


# --- screening ----


def screen(db_path: Path, spec: ScreenSpec, default_as_of: date, *, max_price_age_days: int | None) -> dict[str, Any]:
    as_of = spec.as_of or default_as_of
    loaded = load_universe(db_path, as_of, spec.universe.symbols, max_price_age_days=max_price_age_days)
    outcome = run_screen(spec, [s.view for s in loaded.securities])
    stale = {s.view.ticker: s.freshness for s in loaded.securities if s.freshness["price_metrics_blocked"]}
    return {
        "screen": spec.model_dump(exclude_defaults=False),
        "as_of": as_of,
        "universe": {
            "scope": "requested symbols that are stored" if spec.universe.symbols else "all stored securities",
            "securities_screened": len(loaded.securities),
            "requested_but_not_in_database": loaded.not_in_database,
            "skipped_without_cik": loaded.skipped_without_cik,
            "full_market_universe_loaded": False,
        },
        "freshness_policy": {"max_price_age_days": max_price_age_days, "securities_with_stale_prices": stale},
        **outcome,
    }
