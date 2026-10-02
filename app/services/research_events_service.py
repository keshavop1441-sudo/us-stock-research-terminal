"""Identity resolution and filing-event evidence straight from the SEC (read-only; nothing is written to the database).

* ``resolve_symbols``: ticker -> CIK/name/exchange via the SEC ``company_tickers_exchange`` document (the identity
  authority). A symbol that is absent or ambiguous is reported as unresolved; nothing is guessed.
* ``fetch_filing_events``: the issuer's recent filings from SEC ``submissions``, classified deterministically into
  evidence sections by form type and 8-K item code (``app.research.evidence``). Rate limiting, the identifying
  User-Agent and 403 handling are the shared ``SecHttpClient`` rules.
"""

from datetime import date, datetime
from typing import Any

from app.ingestion import sec_normalize as sn
from app.ingestion.errors import IngestionError
from app.ingestion.sec_http import SecHttpClient
from app.ingestion.sec_source import SUBMISSIONS_URL, fetch_submissions, fetch_ticker_map
from app.models.symbols import canonical_symbol
from app.research.evidence import Company, EvidenceItem, classify_submissions


def resolve_symbols(sec: SecHttpClient, symbols: list[str]) -> dict[str, Any]:
    """One SEC request for all symbols. Returns {resolved: {...}, unresolved: {...}, provenance}."""
    retrieval = fetch_ticker_map(sec)
    ticker_map = sn.parse_ticker_map(retrieval.payload)  # type: ignore[arg-type]
    resolved: dict[str, Any] = {}
    unresolved: dict[str, Any] = {}
    for symbol in symbols:
        try:
            entry = sn.resolve(ticker_map, symbol)
        except IngestionError as exc:
            unresolved[symbol] = {"kind": exc.kind, "message": str(exc)}
            continue
        resolved[symbol] = {"ticker": entry.ticker, "cik": entry.cik, "name": entry.name, "exchange": entry.exchange}
    return {
        "resolved": resolved,
        "unresolved": unresolved,
        "provenance": {
            "provider": "sec",
            "dataset": retrieval.dataset,
            "url": retrieval.url,
            "retrieved_at": retrieval.retrieved_at,
            "content_hash": retrieval.hash,
        },
    }


def fetch_filing_events(
    sec: SecHttpClient, company: Company, *, since: date
) -> tuple[dict[str, list[EvidenceItem]], dict[str, Any]]:
    """({section: items}, window). Raises ``IngestionError`` subclasses (403, 404, ...) for the caller to record."""
    assert company.cik is not None
    retrieval = fetch_submissions(sec, company.cik)
    items, window = classify_submissions(
        retrieval.payload,  # type: ignore[arg-type]
        company,
        since=since,
        retrieved_at=retrieval.retrieved_at,
        source_url=SUBMISSIONS_URL.format(cik=company.cik),
    )
    for section in items.values():
        for index, item in enumerate(section):
            section[index] = item.model_copy(
                update={"provenance": {**item.provenance, "submissions_content_hash": retrieval.hash}}
            )
    window["retrieved_at"] = retrieval.retrieved_at
    return items, window


def events_report(sec: SecHttpClient, symbols: list[str], *, since: date, now: datetime) -> dict[str, Any]:
    """Filing-event evidence for several symbols resolved straight from the SEC."""
    resolution = resolve_symbols(sec, symbols)
    companies: dict[str, Any] = {}
    for symbol, entry in resolution["resolved"].items():
        company = Company(ticker=canonical_symbol(entry["ticker"]), cik=entry["cik"], name=entry["name"])
        try:
            sections, window = fetch_filing_events(sec, company, since=since)
        except IngestionError as exc:
            companies[symbol] = {
                "company": company.model_dump(),
                "status": "UNAVAILABLE",
                "error": {"kind": exc.kind, "message": str(exc)},
            }
            continue
        companies[symbol] = {
            "company": company.model_dump(),
            "status": "OK",
            "window": window,
            "sections": {name: [i.model_dump() for i in items] for name, items in sorted(sections.items())},
        }
    return {
        "since": since,
        "generated_at": now,
        "identity_provenance": resolution["provenance"],
        "companies": companies,
        "unresolved": resolution["unresolved"],
    }
