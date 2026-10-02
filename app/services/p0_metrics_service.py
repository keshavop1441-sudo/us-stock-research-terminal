"""Derived metrics and data validation for the P0 symbols, computed AT READ TIME from stored raw facts and prices.

Nothing computed here is written back (CLAUDE.md: raw facts only, no per-ratio tables). The inputs are exactly what the
database holds, so a metric is reproducible from the rows and their provenance.
"""

from collections.abc import Iterable
from datetime import date
from pathlib import Path

from app.database import access
from app.ingestion.manifest import MANIFEST, P0Security, primary_symbol_for
from app.models.symbols import canonical_symbol
from app.screening.fundamentals import FY_KINDS, Fact, StatementIndex
from app.screening.snapshot import CORE_LINES, IssuerSnapshot, ListingInputs, build_snapshot


def _designate_primary(tickers: list[str], loaded: set[str]) -> tuple[str, str]:
    """(primary listing, how it was designated). Only the audited P0 manifest designates a primary listing; for any
    other issuer the first loaded listing (alphabetical) is used and the packet says it was NOT designated."""
    static = {s.symbol for s in MANIFEST}
    known = sorted(t for t in tickers if t in static)
    if known:
        primary, how = primary_symbol_for(known[0]), "P0_MANIFEST"
    else:
        primary, how = sorted(tickers)[0], "UNDESIGNATED_FIRST_LISTING"
    if primary not in loaded:  # the designated primary failed to load: say so via the flag below
        return sorted(loaded)[0], f"FALLBACK_{how}_PRIMARY_NOT_LOADED"
    return primary, how


def compute_snapshots(
    db_path: Path,
    as_of: date,
    manifest: tuple[P0Security, ...] = MANIFEST,
    *,
    symbols: Iterable[str] | None = None,
) -> dict[str, IssuerSnapshot]:
    """One snapshot per issuer (CIK) that has at least one stored listing among ``symbols`` (default: the manifest)."""
    wanted = {canonical_symbol(s) for s in symbols} if symbols is not None else {s.symbol for s in manifest}
    snapshots: dict[str, IssuerSnapshot] = {}
    with access.reader(db_path) as r:
        securities = r.securities_of_issuers()
        # Every STORED listing of a requested issuer is loaded, not only the requested ticker: the issuer-level cap
        # is the designated primary listing's figure (asking for GOOG alone must still use GOOGL's quote when stored).
        wanted_ciks = {row["cik"] for row in securities.iter_rows(named=True) if row["cik"] and row["ticker"] in wanted}
        by_cik: dict[str, list[dict]] = {}
        for row in securities.iter_rows(named=True):
            if row["cik"] in wanted_ciks:
                by_cik.setdefault(row["cik"], []).append(row)
        for cik, rows in by_cik.items():
            facts = [
                Fact(
                    taxonomy=f["taxonomy"], concept=f["concept"], unit=f["unit"], value=f["value"],
                    period_start=f["period_start"], period_end=f["period_end"], filed=f["filed_date"],
                    accession=f["accession_no"], form=f["form"], fiscal_year=f["fiscal_year"],
                    fiscal_period=f["fiscal_period"], frame=f["frame"],
                )
                for f in r.financial_facts(cik).iter_rows(named=True)
                if f["accession_no"] and f["filed_date"]
            ]  # fmt: skip
            listings = []
            for row in rows:
                bars = [
                    (b["trade_date"], b["open"], b["high"], b["low"], b["close"])
                    for b in r.price_history(row["security_id"]).iter_rows(named=True)
                ]
                quotes = r.market_quotes(row["security_id"])
                quote = quotes.row(0, named=True) if quotes.height else None
                listings.append(
                    ListingInputs(
                        symbol=row["ticker"],
                        bars=bars,
                        quote_cap=quote["market_cap"] if quote else None,
                        quote_date=quote["quote_date"] if quote else None,
                        quote_year_high=quote["year_high"] if quote else None,
                    )
                )
            primary, designation = _designate_primary([r["ticker"] for r in rows], {lst.symbol for lst in listings})
            notes = r.fact_support_notes(cik)
            unavailable = next((n for n in notes if n.startswith("UNSUPPORTED_TAXONOMY:")), None)
            tickers = {x["ticker"] for x in rows}
            multi_class = any("multi_class" in s.roles for s in MANIFEST if s.symbol in tickers)
            snap = build_snapshot(
                cik, facts, listings, primary, as_of, multi_class=multi_class, facts_unavailable=unavailable
            )
            snap.cross_checks["primary_listing"] = primary
            snap.cross_checks["primary_listing_designation"] = designation
            snap.cross_checks["listings"] = sorted(lst.symbol for lst in listings)
            snapshots[cik] = snap
    return snapshots


def core_field_cells(snapshots: dict[str, IssuerSnapshot]) -> dict[str, dict[str, bool]]:
    """cik -> {core line: has a value}. Core = revenue, net income to common, operating cash flow, equity (YAML A5)."""
    out: dict[str, dict[str, bool]] = {}
    for cik, snap in snapshots.items():
        out[cik] = {line: snap.lines.get(line, {}).get("value") is not None for line in CORE_LINES}
    return out


def fiscal_year_coverage(db_path: Path, ciks: list[str]) -> dict[str, dict[str, int]]:
    """cik -> {line: number of distinct fiscal-year periods stored} for the flow lines (YAML A4: at least 4)."""
    out: dict[str, dict[str, int]] = {}
    with access.reader(db_path) as r:
        for cik in ciks:
            facts = [
                Fact(f["taxonomy"], f["concept"], f["unit"], f["value"], f["period_start"], f["period_end"],
                     f["filed_date"], f["accession_no"], f["form"], f["fiscal_year"], f["fiscal_period"], f["frame"])
                for f in r.financial_facts(cik).iter_rows(named=True)
                if f["accession_no"] and f["filed_date"]
            ]  # fmt: skip
            index = StatementIndex(facts)
            out[cik] = {
                line: len({f.period_end for f in index.all_facts(line) if f.kind in FY_KINDS})
                for line in ("revenue", "net_income_to_common", "operating_cash_flow")
            }
    return out


def validation(db_path: Path) -> dict[str, object]:
    """Integrity violations, table counts and the retrieval summary, read from the committed database."""
    with access.reader(db_path) as r, access.quality(db_path) as q:
        return {
            "schema_version": r.schema_version(),
            "table_counts": r.table_counts(),
            "integrity": q.integrity_report(),
            "sources": q.source_summary().to_dicts(),
            "securities": r.securities_of_issuers().to_dicts(),
        }
