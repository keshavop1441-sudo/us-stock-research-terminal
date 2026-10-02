"""Build the evidence packet for ONE stored company (see ``app.research.evidence`` for the contract).

Numbers come only from ``app.screening`` through ``research_query_service`` (never from the model, never re-derived
here); filing events come from SEC ``submissions`` through ``research_events_service``. Whatever this engine cannot
provide is listed as a section limit with guidance, never filled with a guess.
"""

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.ingestion.errors import IngestionError
from app.ingestion.sec_http import SecHttpClient
from app.research import freshness as fr
from app.research.envelope import metric_dict
from app.research.evidence import Company, EvidenceItem, empty_packet, item_id, set_section
from app.research.format import money, render
from app.screening.metrics import MetricResult, MetricState
from app.services import research_events_service as ev
from app.services.research_query_service import LoadedSecurity, load_universe

DEFAULT_EVENT_WINDOW_DAYS = 365
WEB_GUIDANCE = (
    "Not collected by this engine. Claude may use web research, but every item must name the publisher, URL and "
    "publication date, be kept separate from engine-derived numbers, and be labelled as unverified by the engine."
)
SECTION_NOTES = {
    "contracts_customers": "Filing-based only (8-K Items 1.01/1.02: agreement entered into / terminated; type NOT "
    "determined). Customer wins and contract values not reported on an 8-K need web research with attribution.",
    "partnerships": WEB_GUIDANCE,
    "mna": "Filing-based only (8-K Item 2.01, tender-offer/merger proxy/S-4/425 forms). Rumours and announced "
    "deals not yet filed need web research with attribution.",
    "legal_regulatory": "Filing-based only (8-K Items 1.03/1.05/3.01/4.02). Litigation and regulatory actions in "
    "10-K/10-Q legal-proceedings sections are not parsed; use the filing text or web research with attribution.",
    "government_awards": WEB_GUIDANCE + " Suggested public source: usaspending.gov (recipient entities differ from "
    "the issuer; match carefully).",
    "news_events": WEB_GUIDANCE,
    "ownership": "Filing-level only (Schedule 13D/13G filings listed). Institutional (13F) holder detail is not "
    "collected; positions and percentages are NOT given.",
    "insiders": "Filing-level only (Forms 3/4/5/144 listed with dates). Transaction details (buy/sell, price, shares) "
    "are NOT parsed by this engine; open the filing URL before characterising any insider activity.",
}
LIMITS = (
    "The engine does not compute or judge price/volume reaction to events; event timing is reported, causality is not.",
    "Metrics describe the latest fiscal year and TTM from SEC XBRL facts; single quarters are not exposed.",
    "Returns are PRICE returns (no dividends); total return is not produced.",
    "Only securities that were ingested are available; the full market universe is not loaded.",
)


def _items_for(
    section: str,
    company: Company,
    source_type: str,
    as_of: date | None,
    results: dict[str, MetricResult],
    names: tuple[str, ...],
    title: str,
    provenance: dict[str, Any],
    extra: dict[str, Any] | None = None,
    caveats: list[str] | None = None,
) -> EvidenceItem:
    present = {n: results.get(n) for n in names}
    content = title + ": " + "; ".join(f"{n.replace('_', ' ')} {render(n, r)}" for n, r in present.items())
    return EvidenceItem(
        id=item_id(section, company.ticker, title, as_of),
        section=section,
        company=company,
        source="derived",
        source_type=source_type,  # type: ignore[arg-type]
        as_of=as_of,
        content=content,
        data={"metrics": {n: metric_dict(r) for n, r in present.items() if r is not None}, **(extra or {})},
        provenance=provenance,
        caveats=caveats or [],
    )


def _missing(results: dict[str, MetricResult], names: tuple[str, ...]) -> list[dict[str, Any]]:
    out = []
    for name in names:
        r = results.get(name)
        if r is None or not r.ok:
            out.append(
                {
                    "metric": name,
                    "state": r.state.value if r else "MISSING_INPUT",
                    "reason": r.reason if r else "NOT_PRODUCED",
                }
            )
    return out


def _prov(item: LoadedSecurity, metrics: tuple[str, ...]) -> dict[str, Any]:
    refs: list[dict[str, Any]] = []
    for name in metrics:
        for ref in item.view.provenance.get(name, []):
            if ref not in refs:
                refs.append(ref)
    return {"inputs": refs}


def _annual_report_item(item: LoadedSecurity, company: Company) -> EvidenceItem | None:
    lines = item.snapshot.lines
    wanted = ("revenue", "net_income_to_common", "diluted_eps", "operating_cash_flow", "capex")
    known = {k: lines[k] for k in wanted if lines.get(k, {}).get("value") is not None}
    if not known:
        return None
    pieces = []
    for key, rec in known.items():
        value = money(rec["value"]) if key != "diluted_eps" else f"${rec['value']:,.2f}"
        pieces.append(f"{key.replace('_', ' ')} {value}")
    first = next(iter(known.values()))
    fy = lines.get("_fiscal_year_end", {})
    return EvidenceItem(
        id=item_id("earnings", company.ticker, "latest_fiscal_year", fy.get("value")),
        section="earnings",
        company=company,
        source="sec",
        source_type="SEC_XBRL_FACTS",
        as_of=date.fromisoformat(fy["value"]) if fy.get("value") else None,
        filed=date.fromisoformat(first["filed"]) if first.get("filed") else None,
        content=f"Latest fiscal year reported: {'; '.join(pieces)}",
        data={"lines": known, "fiscal_year": fy.get("fiscal_year")},
        provenance={
            "provider": "sec",
            "dataset": "companyfacts",
            "inputs": [{"line": k, **v} for k, v in known.items()],
        },
        caveats=["Annual figures from the latest filing that reports them; quarterly results are in the 10-Q/8-K."],
    )


def build_packet(
    db_path: Path,
    symbol: str,
    as_of: date,
    *,
    sec: SecHttpClient | None,
    now: datetime,
    collect_events: bool = True,
    event_window_days: int = DEFAULT_EVENT_WINDOW_DAYS,
    max_price_age_days: int | None = fr.DEFAULT_MAX_PRICE_AGE_DAYS,
) -> dict[str, Any] | None:
    """The packet, or None if ``symbol`` is not in the database (the caller tells the user to ingest it first)."""
    loaded = load_universe(db_path, as_of, [symbol], max_price_age_days=max_price_age_days)
    if not loaded.securities:
        return None
    item = loaded.securities[0]
    view, snap = item.view, item.snapshot
    company = Company(ticker=view.ticker, cik=view.cik, name=view.name)
    packet = empty_packet(company, as_of, now)
    packet["limits"] = list(LIMITS)
    packet["freshness"] = item.freshness
    unavailable = snap.coverage.get("fundamentals", "OK")
    issuer, listing = view.issuer, view.listing

    # identity
    row = item.listing_row
    identity = EvidenceItem(
        id=item_id("identity", company.ticker),
        section="identity",
        company=company,
        source="sec",
        source_type="SEC_IDENTITY",
        as_of=as_of,
        content=(
            f"{view.name or company.ticker} ({company.ticker}), CIK {view.cik}, exchange {row.get('exchange') or 'N/A'}"
        ),
        data={
            "listings_of_issuer": snap.cross_checks.get("listings", [view.ticker]),
            "primary_listing": snap.cross_checks.get("primary_listing"),
            "primary_listing_designation": snap.cross_checks.get("primary_listing_designation"),
            "classification_nasdaq": {
                "sector": view.nasdaq.sector,
                "industry": view.nasdaq.industry,
                "source": "nasdaq",
            }
            if view.nasdaq
            else None,
            "classification_sec_sic": {"code": view.sic.code, "source": "sec"} if view.sic else None,
            "fundamentals_coverage": unavailable,
            "taxonomy_note": "Nasdaq sector/industry and SEC SIC are different taxonomies; GICS is not used.",
        },
        provenance={"provider": "sec", "retrievals": item.sources},
    )
    set_section(packet, "identity", [identity])

    # price
    bar_note = ["Closes are split-adjusted, not dividend-adjusted; returns are PRICE returns (no dividends)."]
    horizon = (
        "price_return_1m",
        "price_return_3m",
        "price_return_6m",
        "price_return_1y",
        "price_return_3y",
        "price_return_5y",
    )
    ranges = ("drawdown_from_52w_high", "drawdown_from_52w_closing_high", "high_52w", "high_52w_close")
    price_items = [
        _items_for(
            "price",
            company,
            "DERIVED_METRIC",
            item.freshness["last_price_bar"],
            listing,
            horizon,
            "Price returns",
            {"price_retrieval": item.sources.get("price")},
            caveats=bar_note,
        ),
        _items_for(
            "price",
            company,
            "DERIVED_METRIC",
            item.freshness["last_price_bar"],
            listing,
            ranges,
            "52-week range",
            {"price_retrieval": item.sources.get("price")},
            extra={"last_close": item.last_close},
            caveats=bar_note,
        ),
    ]
    set_section(
        packet,
        "price",
        price_items,
        missing=_missing(listing, horizon + ranges),
        status="OK" if item.freshness["last_price_bar"] else "UNAVAILABLE",
    )

    # fundamentals / balance sheet / valuation
    growth = ("revenue_growth_yoy", "eps_growth_yoy", "fcf_growth_yoy", "revenue_growth_ttm_yoy")
    margins = (
        "gross_margin",
        "operating_margin",
        "net_margin",
        "gross_margin_change_yoy",
        "operating_margin_change_yoy",
        "net_margin_change_yoy",
    )
    cash = ("fcf", "revenue_ttm", "diluted_eps_ttm")
    sheet = (
        "total_debt",
        "net_debt",
        "debt_to_equity",
        "debt_to_equity_fy",
        "debt_to_equity_prior_fy",
        "debt_to_equity_change_yoy",
        "shares_outstanding",
    )
    value = ("issuer_market_cap", "price_to_sales", "price_to_sales_ttm")
    fund_names = growth + margins + cash
    if unavailable.startswith("UNAVAILABLE"):
        reason = unavailable.split(":", 1)[1]
        why = f"Fundamentals are UNSUPPORTED for this issuer ({reason}): no US-GAAP facts are invented."
        for name in ("fundamentals", "balance_sheet"):
            set_section(
                packet,
                name,
                [],
                status="UNSUPPORTED",
                missing=[{"metric": "*", "state": "MISSING_INPUT", "reason": reason}],
                guidance=why,
            )
    else:
        prov_f = lambda names: _prov(item, names)  # noqa: E731
        set_section(
            packet,
            "fundamentals",
            [
                _items_for("fundamentals", company, "DERIVED_METRIC", snap.as_of, issuer, names, title, prov_f(names))
                for names, title in ((growth, "Growth"), (margins, "Margins"), (cash, "Cash flow and earnings"))
            ],
            missing=_missing(issuer, fund_names),
            status="OK" if snap.coverage.get("fundamentals") == "OK" else "UNAVAILABLE",
        )
        set_section(
            packet,
            "balance_sheet",
            [
                _items_for(
                    "balance_sheet",
                    company,
                    "DERIVED_METRIC",
                    snap.as_of,
                    issuer,
                    sheet,
                    "Balance sheet and leverage",
                    prov_f(sheet),
                    extra={"lines": {k: v for k, v in snap.lines.items() if not k.startswith("_")}},
                )
            ],
            missing=_missing(issuer, sheet),
            status="OK" if snap.coverage.get("fundamentals") == "OK" else "UNAVAILABLE",
        )
    pe = {
        "price_to_earnings": listing.get(
            "price_to_earnings", MetricResult(MetricState.MISSING_INPUT, None, "NOT_PRODUCED")
        )
    }
    valuation_item = _items_for(
        "valuation",
        company,
        "DERIVED_METRIC",
        snap.as_of,
        {**issuer, **pe},
        (*value, "price_to_earnings"),
        "Valuation",
        {**_prov(item, value + ("price_to_earnings",)), "quote_retrieval": item.sources.get("quote")},
        caveats=[
            "Market cap is the provider-quoted figure of the issuer's primary listing; P/S uses fiscal-year revenue "
            "(price_to_sales_ttm uses TTM revenue)."
        ],
    )
    set_section(
        packet, "valuation", [valuation_item], missing=_missing({**issuer, **pe}, (*value, "price_to_earnings"))
    )

    # earnings (latest annual figures) – 8-K 2.02 filings are added below
    annual = None if unavailable.startswith("UNAVAILABLE") else _annual_report_item(item, company)
    sections: dict[str, list[EvidenceItem]] = {}
    window: dict[str, Any] | None = None
    reason: str | None = None
    if not collect_events:
        reason = "EVENTS_NOT_REQUESTED"
    elif sec is None:
        reason = "SEC_USER_AGENT_MISSING"
    else:
        try:
            sections, window = ev.fetch_filing_events(sec, company, since=as_of - timedelta(days=event_window_days))
        except IngestionError as exc:
            reason = f"{exc.kind}: {exc}"
    packet["event_window"] = window
    for name in (
        "filings",
        "earnings",
        "insiders",
        "ownership",
        "mna",
        "legal_regulatory",
        "contracts_customers",
        "other",
    ):
        found = list(sections.get(name, []))
        if name == "earnings" and annual:
            found.append(annual)
        guidance = SECTION_NOTES.get(name)
        if reason is not None:
            set_section(
                packet,
                name,
                [annual] if name == "earnings" and annual else [],
                status="OK" if name == "earnings" and annual else "NOT_COLLECTED",
                missing=[{"source": "sec submissions", "reason": reason}],
                guidance=guidance,
            )
        else:
            set_section(packet, name, found, status="OK" if found else "NONE_FOUND", guidance=guidance)
    for name in ("news_events", "partnerships", "government_awards"):
        set_section(packet, name, [], status="NOT_COLLECTED", guidance=SECTION_NOTES[name])
    return packet
