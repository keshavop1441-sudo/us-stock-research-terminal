"""SEC JSON -> validated records. Pure functions: no network, no database.

Three documents are understood:

* ``company_tickers_exchange.json`` -> ``TickerEntry`` per ticker (CIK normalised by ``normalize_cik`` only).
* ``submissions/CIK##########.json`` -> ``IssuerProfile`` (name, SIC, nominal fiscal year end, tickers) and
  ``FilingRecord`` rows for annual/quarterly reports and their amendments (accession, form, filing date, report date,
  primary document).
* ``companyfacts/CIK##########.json`` -> ``RawFactPoint`` for the allow-listed P0 concepts (``app.models.concepts``).

Nothing is dropped silently: every point/row that is not stored is counted in ``NormalizationStats`` under a reason, and
malformed ones are kept in ``rejects`` so the report can list them.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from app.ingestion.errors import IdentityError, MalformedResponseError
from app.models.concepts import (
    ACCOUNTING_FORMS,
    INDEXED_FORMS,
    KNOWN_UNSUPPORTED_ACCOUNTING_TAXONOMIES,
    SPEC_BY_LINE,
    SPEC_BY_TAG,
    SUPPORTED_ACCOUNTING_TAXONOMIES,
)
from app.models.identifiers import InvalidCikError, normalize_cik
from app.models.periods import parse_fiscal_year_end
from app.models.records import FilingRecord
from app.models.sec_facts import ARCHIVE_URL, RawFactPoint, parse_companyfacts_point
from app.models.symbols import canonical_symbol

_TICKER_FIELDS = ["cik", "name", "ticker", "exchange"]
# Named tuple, not `except ValueError, TypeError:` (3.14-only syntax; see app/database/locking.py).
_BAD_ROW = (ValueError, TypeError)


@dataclass(frozen=True)
class TickerEntry:
    cik: str
    name: str
    ticker: str  # canonical (SEC spelling: BRK-B)
    exchange: str | None


def parse_ticker_map(payload: dict) -> dict[str, list[TickerEntry]]:
    """ticker -> entries. More than one entry for a ticker (or several CIKs) is kept so the caller can refuse
    ambiguity."""
    if payload.get("fields") != _TICKER_FIELDS or not isinstance(payload.get("data"), list):
        raise MalformedResponseError(f"company_tickers_exchange: unexpected shape (fields={payload.get('fields')!r})")
    out: dict[str, list[TickerEntry]] = {}
    for row in payload["data"]:
        try:
            cik, name, ticker, exchange = row
            entry = TickerEntry(normalize_cik(cik), str(name), canonical_symbol(ticker), exchange or None)
        except _BAD_ROW:  # InvalidCikError/InvalidTickerError are ValueErrors
            continue  # a malformed row is not a requested symbol; resolve() fails loudly if a wanted ticker is absent
        out.setdefault(entry.ticker, []).append(entry)
    return out


def resolve(ticker_map: dict[str, list[TickerEntry]], symbol: str, expected_cik: str | None = None) -> TickerEntry:
    """Exactly one CIK for ``symbol`` or an ``IdentityError``. A disagreement with the audit's CIK is also an error."""
    entries = ticker_map.get(canonical_symbol(symbol), [])
    ciks = {e.cik for e in entries}
    if not entries:
        raise IdentityError(f"{symbol}: not in the SEC ticker map (no CIK; nothing is guessed)")
    if len(ciks) > 1:
        raise IdentityError(f"{symbol}: ambiguous, maps to several CIKs {sorted(ciks)}")
    if expected_cik is not None and entries[0].cik != expected_cik:
        raise IdentityError(f"{symbol}: SEC says CIK {entries[0].cik} but the Phase 2 audit recorded {expected_cik}")
    return entries[0]


@dataclass(frozen=True)
class IssuerProfile:
    cik: str
    name: str
    sic: str | None
    sic_description: str | None
    fiscal_year_end: tuple[int, int] | None  # nominal (month, day); actual period ends come from filings
    tickers: tuple[str, ...]
    exchanges: tuple[str | None, ...]
    former_names: tuple[str, ...]


def parse_submissions(payload: dict, cik: str) -> tuple[IssuerProfile, list[FilingRecord]]:
    try:
        reported = normalize_cik(payload["cik"])
    except (KeyError, InvalidCikError) as exc:
        raise MalformedResponseError(f"submissions: missing or invalid cik ({exc})") from exc
    if reported != cik:
        raise MalformedResponseError(f"submissions: asked for CIK {cik} but the document is for {reported}")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise MalformedResponseError("submissions: no entity name")
    try:
        fye = parse_fiscal_year_end(payload.get("fiscalYearEnd"))
    except ValueError:
        fye = None  # malformed nominal FYE is not worth failing identity; it stays unknown (NULL), never guessed
    profile = IssuerProfile(
        cik=cik,
        name=name,
        sic=str(payload["sic"]) if payload.get("sic") else None,
        sic_description=payload.get("sicDescription") or None,
        fiscal_year_end=fye,
        tickers=tuple(canonical_symbol(t) for t in payload.get("tickers") or []),
        exchanges=tuple(payload.get("exchanges") or []),
        former_names=tuple(f["name"] for f in payload.get("formerNames") or [] if isinstance(f, dict) and "name" in f),
    )
    recent = (payload.get("filings") or {}).get("recent") or {}
    columns = ["accessionNumber", "form", "filingDate", "reportDate", "primaryDocument"]
    arrays = {c: recent.get(c) or [] for c in columns}
    if len({len(v) for v in arrays.values()}) > 1:
        raise MalformedResponseError("submissions: filings.recent arrays have different lengths")
    filings: list[FilingRecord] = []
    for accession, form, filed, report, document in zip(*(arrays[c] for c in columns), strict=True):
        if form not in INDEXED_FORMS:
            continue
        filings.append(
            FilingRecord(
                accession_no=accession,
                cik=cik,
                form=form,
                filing_date=date.fromisoformat(filed),
                report_date=date.fromisoformat(report) if report else None,
                primary_document=document or None,
                url=ARCHIVE_URL.format(cik_number=int(cik), accession_nodash=accession.replace("-", ""))
                + (document or ""),
            )
        )
    return profile, filings


@dataclass
class FactsParse:
    points: list[RawFactPoint] = field(default_factory=list)
    rejects: list[dict[str, object]] = field(default_factory=list)  # malformed points: kept, reported, never stored
    skipped: Counter = field(default_factory=Counter)  # reason -> count (legitimately not stored)
    duplicates: int = 0  # the same logical key more than once inside the document
    entity_name: str | None = None
    taxonomies: tuple[str, ...] = ()
    lines_present: frozenset[str] = frozenset()
    # set when the document has an unsupported accounting taxonomy and NO supported one: nothing is stored then
    unsupported_taxonomy: str | None = None


def parse_companyfacts(payload: dict, cik: str) -> FactsParse:
    try:
        reported = normalize_cik(payload["cik"])
    except (KeyError, InvalidCikError) as exc:
        raise MalformedResponseError(f"companyfacts: missing or invalid cik ({exc})") from exc
    if reported != cik:
        raise MalformedResponseError(f"companyfacts: asked for CIK {cik} but the document is for {reported}")
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        raise MalformedResponseError("companyfacts: no 'facts' object")
    result = FactsParse(entity_name=payload.get("entityName"), taxonomies=tuple(sorted(facts)))
    unsupported = sorted(set(facts) & KNOWN_UNSUPPORTED_ACCOUNTING_TAXONOMIES)
    if unsupported and not set(facts) & SUPPORTED_ACCOUNTING_TAXONOMIES:
        # e.g. TSM: taxonomies ['dei', 'ifrs-full', 'srt']. Not a failure and not mapped: dei cover-page counts of a
        # foreign issuer (ordinary shares vs ADS) are not reliable on their own either, so nothing is kept.
        result.unsupported_taxonomy = unsupported[0]
        return result
    seen: set[tuple] = set()
    for (taxonomy, tag), spec in sorted(SPEC_BY_TAG.items()):
        concept = (facts.get(taxonomy) or {}).get(tag)
        if not concept:
            continue
        for unit, points in (concept.get("units") or {}).items():
            if unit != spec.unit:
                result.skipped[f"UNEXPECTED_UNIT:{unit}"] += len(points)
                continue
            for point in points:
                try:
                    raw = parse_companyfacts_point(cik, taxonomy, tag, unit, point)
                except _BAD_ROW as exc:
                    result.rejects.append({"concept": tag, "point": point, "error": str(exc)})
                    continue
                if raw.form not in ACCOUNTING_FORMS:
                    result.skipped[f"FORM:{raw.form}"] += 1
                    continue
                if not raw.accession:  # A13: every stored SEC fact carries its accession, filing date and form
                    result.rejects.append({"concept": tag, "point": point, "error": "MISSING_PROVENANCE:accession"})
                    continue
                key = (taxonomy, tag, unit, raw.period_start, raw.period_end, raw.accession)
                if key in seen:
                    result.duplicates += 1
                seen.add(key)
                result.points.append(raw)
                result.lines_present |= {spec.line}
    result.lines_present = frozenset(result.lines_present)
    return result


def known_lines() -> frozenset[str]:
    return frozenset(SPEC_BY_LINE)
