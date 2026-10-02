"""All derived metrics of one issuer at one date, assembled from stored raw facts and prices. Pure, no I/O.

Nothing here defines a metric: every number comes from ``app.screening.metrics`` (the Phase 2 metric dictionary)
applied to inputs selected by ``app.screening.fundamentals``. Every metric is a ``MetricResult``; a state other than
OK means "no value for this reason" and is never shown as 0.

Valuation uses ISSUER-level inputs only: ``issuer_market_cap`` (one value per CIK from the designated primary listing)
divided by issuer revenue. Per-listing metrics (price returns, drawdown, P/E) use that listing's own prices; multi-class
issuers carry the flag ``MULTI_CLASS_PER_SHARE_BASIS_UNVERIFIED`` on P/E because per-class EPS comparability is
unverified. Returns are PRICE returns (no dividends); total return is UNAVAILABLE and not produced.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from app.screening import metrics as m
from app.screening.fundamentals import FY_KINDS, StatementIndex
from app.screening.metrics import MetricResult, MetricState

RETURN_HORIZONS = {
    "price_return_1m": {"months": 1},
    "price_return_3m": {"months": 3},
    "price_return_6m": {"months": 6},
    "price_return_1y": {"years": 1},
    "price_return_3y": {"years": 3},
    "price_return_5y": {"years": 5},
}
QUOTE_STALE_DAYS = 5
CORE_LINES = ("revenue", "net_income_to_common", "operating_cash_flow", "equity")  # YAML A5


def _missing(reason: str) -> MetricResult:
    return MetricResult(MetricState.MISSING_INPUT, None, reason)


def _with_flags(result: MetricResult, *flags: str) -> MetricResult:
    return MetricResult(result.state, result.value, result.reason, tuple(dict.fromkeys((*result.flags, *flags))))


def _problem(selection_problem: str | None) -> MetricResult:
    return _missing(selection_problem or "NOT_REPORTED")


@dataclass(frozen=True)
class ListingInputs:
    symbol: str
    bars: Sequence[tuple[date, float | None, float | None, float | None, float | None]]  # date, open, high, low, close
    quote_cap: float | None  # the provider's quoted market cap for THIS listing
    quote_date: date | None
    quote_year_high: float | None = None


@dataclass
class IssuerSnapshot:
    cik: str
    as_of: date
    lines: dict[str, dict[str, object]] = field(default_factory=dict)  # normalised lines with tag/filing provenance
    issuer: dict[str, MetricResult] = field(default_factory=dict)
    listings: dict[str, dict[str, MetricResult]] = field(default_factory=dict)
    cross_checks: dict[str, object] = field(default_factory=dict)

    def all_results(self) -> list[tuple[str, MetricResult]]:
        out = [(name, r) for name, r in self.issuer.items()]
        out += [(f"{sym}:{name}", r) for sym, metrics in self.listings.items() for name, r in metrics.items()]
        return out


def _line_record(index: StatementIndex, line: str, end) -> dict[str, object]:
    sel = index.select(line, end=end, kinds=None)
    if sel.fact is None:
        return {"value": None, "reason": sel.problem}
    return {
        "value": sel.value,
        "tag": sel.tag,
        "accession": sel.fact.accession,
        "form": sel.fact.form,
        "filed": sel.fact.filed.isoformat(),
        "period_end": sel.fact.period_end.isoformat(),
        "flags": list(sel.flags),
    }


def fundamental_metrics(
    index: StatementIndex, as_of: date
) -> tuple[dict[str, MetricResult], dict[str, dict[str, object]]]:
    """Issuer-level metrics that need no price, plus the normalised lines they were built from (with provenance)."""
    out: dict[str, MetricResult] = {}
    lines: dict[str, dict[str, object]] = {}
    fy_end = index.latest_end("revenue", FY_KINDS) or index.latest_end("net_income_to_common", FY_KINDS)

    def fy(line: str):
        return index.select(line, end=fy_end, kinds=FY_KINDS) if fy_end else None

    if fy_end is None:
        reason = _missing("NO_ANNUAL_REVENUE_OR_NET_INCOME")
        for name in (
            "revenue_growth_yoy",
            "eps_growth_yoy",
            "fcf",
            "fcf_growth_yoy",
            "gross_margin",
            "operating_margin",
            "net_margin",
        ):
            out[name] = reason
    else:
        for line in (
            "revenue", "gross_profit", "operating_income", "net_income_to_common", "diluted_eps",
            "operating_cash_flow", "capex",
        ):  # fmt: skip
            sel = fy(line)
            lines[line] = (
                _line_record(index, line, fy_end)
                if sel and sel.fact
                else {"value": None, "reason": sel.problem if sel else "NOT_REPORTED"}
            )
        lines["_fiscal_year_end"] = {
            "value": fy_end.isoformat(),
            "fiscal_year": index.fiscal_year_label("revenue", fy_end),
        }
        revenue = fy("revenue")
        for name, line in (
            ("gross_margin", "gross_profit"),
            ("operating_margin", "operating_income"),
            ("net_margin", "net_income_to_common"),
        ):
            top = fy(line)
            if revenue is None or revenue.fact is None:
                out[name] = _problem(revenue.problem if revenue else None)
            elif top is None or top.fact is None:
                out[name] = _problem(top.problem if top else None)
            else:
                out[name] = m.margin(top.value, revenue.value)
        out["revenue_growth_yoy"] = _growth(index, "revenue", fy_end)
        out["eps_growth_yoy"] = _growth(index, "diluted_eps", fy_end)
        cfo, capex = fy("operating_cash_flow"), fy("capex")
        if cfo and capex and cfo.fact and capex.fact:
            same = cfo.fact.accession == capex.fact.accession
            out["fcf"] = _with_flags(m.free_cash_flow(cfo.value, capex.value), *(() if same else ("MIXED_FILINGS",)))
        else:
            out["fcf"] = m.free_cash_flow(cfo.value if cfo else None, capex.value if capex else None)
        out["fcf_growth_yoy"] = _fcf_growth(index, fy_end)
    # trailing twelve months
    ttm_revenue = index.ttm("revenue")
    out["revenue_ttm"] = ttm_revenue.as_metric()
    out["revenue_growth_ttm_yoy"] = _ttm_growth(index, "revenue", ttm_revenue)
    eps_ttm = index.ttm_eps()
    out["diluted_eps_ttm"] = eps_ttm.as_metric()
    lines["_ttm_revenue"] = (
        {
            "basis": ttm_revenue.ttm.basis,
            "tag": ttm_revenue.ttm.tag,
            "period_end": ttm_revenue.ttm.period_end.isoformat(),
        }
        if ttm_revenue.ttm
        else {"reason": ttm_revenue.reason}
    )
    # balance sheet
    sheet = index.balance_sheet()
    if not sheet.present:
        out["total_debt"] = m.total_debt(None, None, None, balance_sheet_present=False)
    else:
        lines["_balance_sheet"] = {"period_end": sheet.end.isoformat()}  # type: ignore[union-attr]
        for line in ("cash", "equity", "short_term_debt", "current_portion_long_term_debt", "long_term_debt"):
            sel = sheet.values.get(line)
            lines[line] = (
                _line_record(index, line, sheet.end)
                if sel and sel.fact
                else {"value": None, "reason": sel.problem if sel else "NOT_REPORTED"}
            )
        debt_conflict = [
            ln
            for ln in ("short_term_debt", "current_portion_long_term_debt", "long_term_debt")
            if ln in sheet.conflicts
        ]
        if debt_conflict:
            out["total_debt"] = _missing("DEBT_LINE_" + sheet.conflicts[debt_conflict[0]])  # type: ignore[operator]
        else:
            out["total_debt"] = m.total_debt(
                sheet.value("short_term_debt"),
                sheet.value("current_portion_long_term_debt"),
                sheet.value("long_term_debt"),
                balance_sheet_present=True,
                explicit_no_debt_evidence=False,  # P0 ingests no explicit "no debt" disclosure: absence is never zero
            )
    cash_conflict = "cash" in sheet.conflicts or "short_term_investments" in sheet.conflicts
    equity_conflict = "equity" in sheet.conflicts
    out["net_debt"] = (
        _missing("CASH_TAG_CONFLICT")
        if cash_conflict
        else m.net_debt(out["total_debt"], sheet.value("cash"), sheet.value("short_term_investments"))
    )
    out["debt_to_equity"] = (
        _missing("EQUITY_TAG_CONFLICT")
        if equity_conflict
        else m.debt_to_equity(out["total_debt"], sheet.value("equity"))
    )
    shares, share_reason = index.shares_outstanding(as_of)
    out["shares_outstanding"] = (
        m.MetricResult(MetricState.OK, shares) if shares is not None else _missing(share_reason or "?")
    )
    return out, lines


def _growth(index: StatementIndex, line: str, fy_end: date) -> MetricResult:
    pair = index.annual_pair(line, fy_end)
    if pair is None:
        return _missing("NO_FILING_REPORTS_BOTH_YEARS")
    now, prior, _tag = pair
    comparable = m.comparable_year_over_year((now.period_start, now.period_end), (prior.period_start, prior.period_end))
    if not comparable.ok:
        return comparable
    return m.growth_rate(now.value, prior.value)


def _ttm_growth(index: StatementIndex, line: str, current) -> MetricResult:
    if current.ttm is None:
        return current.as_metric()
    prior = index.ttm_prior_year(line, current.ttm)
    if prior.ttm is None:
        return prior.as_metric()
    if "RESTATED_COMPONENT" in current.ttm.flags or any(
        f.startswith("RESTATED") for f in (*current.ttm.flags, *prior.ttm.flags)
    ):
        return MetricResult(MetricState.NOT_COMPARABLE, None, "RESTATED_COMPONENT")
    return _with_flags(m.growth_rate(current.ttm.value, prior.ttm.value), "TTM_COMPONENTS_FROM_SEVERAL_FILINGS")


def _fcf_growth(index: StatementIndex, fy_end: date) -> MetricResult:
    """FCF growth needs operating cash flow AND capex for both years from ONE filing (never mixed vintages)."""
    cfo = index.annual_pair("operating_cash_flow", fy_end)
    capex = index.annual_pair("capex", fy_end)
    if cfo is None or capex is None:
        return _missing("CAPEX_OR_CFO_PAIR_MISSING")
    if cfo[0].accession != capex[0].accession or cfo[1].accession != capex[1].accession:
        return MetricResult(MetricState.NOT_COMPARABLE, None, "CFO_AND_CAPEX_FROM_DIFFERENT_FILINGS")
    now = m.free_cash_flow(cfo[0].value, capex[0].value)
    prior = m.free_cash_flow(cfo[1].value, capex[1].value)
    blocked = m._depends_on(now, prior)  # noqa: SLF001 - same package, reuse the "MISSING wins" rule
    return blocked or m.growth_rate(now.value, prior.value)


def market_metrics(
    listing: ListingInputs, as_of: date, issuer_inputs: dict[str, MetricResult], *, multi_class: bool
) -> dict[str, MetricResult]:
    """Per-listing metrics: price returns, drawdowns, and P/E at this listing's own last close."""
    bars = [b for b in listing.bars if b[0] <= as_of]
    closes = [(d, c) for d, _o, _h, _lo, c in bars]
    out: dict[str, MetricResult] = {
        name: m.lookback_return(closes, as_of, **horizon) for name, horizon in RETURN_HORIZONS.items()
    }
    window = m.high_low_52w([(d, h, lo, c) for d, _o, h, lo, c in bars], as_of)
    last = closes[-1][1] if closes else None
    out["drawdown_from_52w_high"] = m.drawdown_from_high(last, window["high_52w"].value)
    out["drawdown_from_52w_closing_high"] = m.drawdown_from_high(last, window["high_52w_close"].value)
    out["high_52w"], out["high_52w_close"] = window["high_52w"], window["high_52w_close"]
    pe = m.price_to_earnings(
        last, issuer_inputs["diluted_eps_ttm"].value if issuer_inputs["diluted_eps_ttm"].ok else None
    )
    if not issuer_inputs["diluted_eps_ttm"].ok and pe.state is MetricState.MISSING_INPUT and last:
        pe = MetricResult(
            issuer_inputs["diluted_eps_ttm"].state, None, "EPS_TTM:" + str(issuer_inputs["diluted_eps_ttm"].reason)
        )
    out["price_to_earnings"] = _with_flags(pe, *(("MULTI_CLASS_PER_SHARE_BASIS_UNVERIFIED",) if multi_class else ()))
    return out


def build_snapshot(
    cik: str, facts: Sequence, listings: Sequence[ListingInputs], primary_symbol: str, as_of: date
) -> IssuerSnapshot:
    index = StatementIndex(facts)
    snap = IssuerSnapshot(cik=cik, as_of=as_of)
    snap.issuer, snap.lines = fundamental_metrics(index, as_of)
    caps = {lst.symbol: lst.quote_cap for lst in listings}
    cap = m.issuer_market_cap(caps, primary_symbol) if listings else _missing("NO_LISTINGS")
    primary = next((lst for lst in listings if lst.symbol == primary_symbol), None)
    if cap.ok and primary and primary.quote_date and (as_of - primary.quote_date).days > QUOTE_STALE_DAYS:
        cap = _with_flags(cap, f"QUOTE_STALE:{(as_of - primary.quote_date).days}D")
    snap.issuer["issuer_market_cap"] = cap
    revenue_fy = snap.lines.get("revenue", {}).get("value")
    snap.issuer["price_to_sales"] = m.price_to_sales(cap.value if cap.ok else None, revenue_fy)
    if not cap.ok:
        snap.issuer["price_to_sales"] = _with_flags(snap.issuer["price_to_sales"], "MARKET_CAP:" + str(cap.reason))
    snap.issuer["price_to_sales_ttm"] = m.price_to_sales(
        cap.value if cap.ok else None,
        snap.issuer["revenue_ttm"].value if snap.issuer["revenue_ttm"].ok else None,
    )
    for listing in listings:
        snap.listings[listing.symbol] = market_metrics(listing, as_of, snap.issuer, multi_class=len(listings) > 1)
    # cross-checks (YAML M12): provider 52-week high vs ours; issuer cap vs close x single-class dei shares
    for listing in listings:
        ours = snap.listings[listing.symbol]["high_52w"]
        if ours.ok and listing.quote_year_high:
            snap.cross_checks[f"{listing.symbol}:year_high_diff"] = ours.value / listing.quote_year_high - 1
    shares = snap.issuer["shares_outstanding"]
    if primary and shares.ok and cap.ok and primary.bars and len(listings) == 1:
        last_close = max((b for b in primary.bars if b[0] <= as_of), key=lambda b: b[0])[4]
        if last_close:
            snap.cross_checks["market_cap_vs_close_x_dei_shares"] = cap.value / (last_close * shares.value) - 1
    return snap
