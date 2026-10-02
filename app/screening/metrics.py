"""Executable definitions of the derived-metric rules (see docs/data_coverage.yaml, ``metrics`` section).

Pure functions only: no I/O, no database, no provider calls. They exist so the metric dictionary's
"N/A semantics" are unambiguous and tested, and so the future screener cannot reinvent them.

Every function returns a ``MetricResult`` instead of raising or returning a bare number. A result is
usable only when ``state is MetricState.OK``; every other state means "no value", for a stated reason:

    MISSING_INPUT      an input is absent/NaN. Absent data is never replaced by zero (documented exceptions flag it).
    ZERO_DENOMINATOR   the divisor is exactly zero.
    NOT_MEANINGFUL     the arithmetic is defined but the result would mislead (negative EPS -> positive EPS,
                       zero or negative base, loss-making P/E, negative equity, ...).
    NOT_COMPARABLE     the two inputs do not describe comparable things (different period length, currency, ...).

A threshold screen treats any non-OK result as "does not match and cannot be evaluated"; it must never be
turned into 0, +inf or a sign-flipped percentage.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

# Named tuple, not `except TypeError, ValueError:` (3.14-only syntax; see app/database/locking.py).
_NOT_A_NUMBER = (TypeError, ValueError)

# --- result types ---------------------------------------------------------------------------------------------------


class MetricState(StrEnum):
    OK = "OK"
    MISSING_INPUT = "MISSING_INPUT"
    ZERO_DENOMINATOR = "ZERO_DENOMINATOR"
    NOT_MEANINGFUL = "NOT_MEANINGFUL"
    NOT_COMPARABLE = "NOT_COMPARABLE"


@dataclass(frozen=True)
class MetricResult:
    state: MetricState
    value: float | None = None
    reason: str | None = None  # machine-readable code, e.g. "SIGN_CHANGE"
    flags: tuple[str, ...] = ()  # advisory notes on an OK value, e.g. "ASSUMED_ZERO:short_term_investments"

    @property
    def ok(self) -> bool:
        return self.state is MetricState.OK


def _ok(value: float, flags: tuple[str, ...] = ()) -> MetricResult:
    return MetricResult(MetricState.OK, value, None, flags)


def _no(state: MetricState, reason: str) -> MetricResult:
    return MetricResult(state, None, reason)


def _num(value: object) -> float | None:
    """A usable finite number, or None (None, NaN, inf and bool are all 'absent')."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except _NOT_A_NUMBER:
        return None
    return number if math.isfinite(number) else None


def _depends_on(*inputs: MetricResult) -> MetricResult | None:
    """If any input metric is not OK, the derived metric is not computable; MISSING wins over other states."""
    bad = [r for r in inputs if not r.ok]
    if not bad:
        return None
    missing = next((r for r in bad if r.state is MetricState.MISSING_INPUT), None)
    return _no((missing or bad[0]).state, "DEPENDS_ON_UNDEFINED_INPUT")


# --- growth and margins ---------------------------------------------------------------------------------------------


def growth_rate(current: object, prior: object) -> MetricResult:
    """Year-over-year (or any like-for-like) growth as a fraction: ``current / prior - 1``.

    Defined only for a POSITIVE prior value and a non-negative current value. In particular:
      * prior == 0                      -> ZERO_DENOMINATOR (ZERO_BASE): "0 -> positive" is not a growth rate
      * prior < 0                       -> NOT_MEANINGFUL: loss -> profit (SIGN_CHANGE) and loss -> bigger loss
                                           (NEGATIVE_BASE) are both excluded; use a level/turnaround metric instead
      * prior > 0 and current < 0       -> NOT_MEANINGFUL (SIGN_CHANGE): profit -> loss is not a percentage decline
      * prior > 0 and current == 0      -> OK, -1.0 (a complete decline)
    """
    now, before = _num(current), _num(prior)
    if now is None or before is None:
        return _no(MetricState.MISSING_INPUT, "MISSING_VALUE")
    if before == 0:
        return _no(MetricState.ZERO_DENOMINATOR, "ZERO_BASE")
    if before < 0:
        return _no(MetricState.NOT_MEANINGFUL, "SIGN_CHANGE" if now > 0 else "NEGATIVE_BASE")
    if now < 0:
        return _no(MetricState.NOT_MEANINGFUL, "SIGN_CHANGE")
    return _ok(now / before - 1)


def margin(numerator: object, revenue: object) -> MetricResult:
    """``numerator / revenue`` (gross, operating or net margin). A negative margin is a valid, meaningful value."""
    top, revenue_ = _num(numerator), _num(revenue)
    if top is None or revenue_ is None:
        return _no(MetricState.MISSING_INPUT, "MISSING_VALUE")
    if revenue_ == 0:
        return _no(MetricState.ZERO_DENOMINATOR, "ZERO_REVENUE")
    if revenue_ < 0:
        return _no(MetricState.NOT_MEANINGFUL, "NEGATIVE_REVENUE")
    return _ok(top / revenue_)


def gross_profit_from_components(revenue: object, cost_of_revenue: object) -> MetricResult:
    """Gross profit when the filer does not report it: ``revenue - cost_of_revenue``, flagged as derived.

    ``cost_of_revenue`` must be the cost-of-revenue line itself. Total "costs and expenses" is NOT a substitute.
    """
    rev, cost = _num(revenue), _num(cost_of_revenue)
    if rev is None or cost is None:
        return _no(MetricState.MISSING_INPUT, "COST_OF_REVENUE_NOT_REPORTED")
    return _ok(rev - cost, ("DERIVED_GROSS_PROFIT",))


def margin_change(margin_now: MetricResult, margin_prior: MetricResult) -> MetricResult:
    """Change in a margin in percentage points (as a fraction difference). Defined whenever both margins are OK,
    including negative margins: a level difference, not a ratio, so it never divides by a negative base."""
    blocked = _depends_on(margin_now, margin_prior)
    if blocked:
        return blocked
    return _ok(margin_now.value - margin_prior.value)


# --- cash flow and balance sheet ------------------------------------------------------------------------------------


def free_cash_flow(operating_cash_flow: object, capex_payments: object) -> MetricResult:
    """FCF = net cash from operating activities - capital expenditures.

    ``capex_payments`` is the gross payments line for property, plant and equipment
    (us-gaap ``PaymentsToAcquirePropertyPlantAndEquipment``); its sign convention differs between sources, so the
    magnitude is used. A missing capex line is NOT treated as zero capex. No source reports FCF directly.
    """
    cfo, capex = _num(operating_cash_flow), _num(capex_payments)
    if cfo is None:
        return _no(MetricState.MISSING_INPUT, "OPERATING_CASH_FLOW_MISSING")
    if capex is None:
        return _no(MetricState.MISSING_INPUT, "CAPEX_NOT_REPORTED")
    return _ok(cfo - abs(capex))


def total_debt(
    short_term_debt: object,
    current_portion_long_term_debt: object,
    long_term_debt: object,
    finance_lease_liabilities: object = None,
    *,
    balance_sheet_present: bool,
    company_type: str | None = None,
) -> MetricResult:
    """Financial debt = short-term borrowings + current portion of long-term debt + long-term debt + finance leases.

    Operating-lease liabilities are excluded (use a separate, explicitly named metric if wanted).
    XBRL filers omit zero lines, so for an ``industrial`` filer (OpenBB/SEC ``company_type``) with a balance sheet for
    the period an absent COMPONENT counts as zero WITH A FLAG. For any other or unknown ``company_type`` an absent
    component is MISSING_INPUT: the audit found Berkshire Hathaway ("diversified") reports no debt lines at all on a
    $1.2T balance sheet, so "absent" is not evidence of "zero" there. With no balance sheet there is no evidence
    either way -> MISSING_INPUT.
    """
    parts = {
        "short_term_debt": _num(short_term_debt),
        "current_portion_long_term_debt": _num(current_portion_long_term_debt),
        "long_term_debt": _num(long_term_debt),
        "finance_lease_liabilities": _num(finance_lease_liabilities),
    }
    present = {k: v for k, v in parts.items() if v is not None}
    if any(v < 0 for v in present.values()):
        return _no(MetricState.NOT_MEANINGFUL, "NEGATIVE_DEBT_COMPONENT")
    if not balance_sheet_present:
        return _no(MetricState.MISSING_INPUT, "NO_BALANCE_SHEET")
    absent = sorted(k for k, v in parts.items() if v is None and k != "finance_lease_liabilities")
    if absent and company_type != "industrial":
        return _no(MetricState.MISSING_INPUT, "DEBT_LINES_ABSENT_NON_INDUSTRIAL_OR_UNKNOWN_FILER")
    if not present:
        return _ok(0.0, ("NO_DEBT_LINES_REPORTED_ASSUMED_ZERO",))
    return _ok(sum(present.values()), tuple(f"ASSUMED_ZERO:{k}" for k in absent))


def net_debt(debt: MetricResult, cash_and_equivalents: object, short_term_investments: object) -> MetricResult:
    """Net debt = total debt - (cash and equivalents + short-term investments). Negative means net cash.

    Long-term investments are excluded (not readily available to repay debt). Cash must be reported; an absent
    short-term-investments line counts as zero WITH A FLAG (many companies hold none).
    """
    blocked = _depends_on(debt)
    if blocked:
        return blocked
    cash, sti = _num(cash_and_equivalents), _num(short_term_investments)
    if cash is None:
        return _no(MetricState.MISSING_INPUT, "CASH_NOT_REPORTED")
    flags = debt.flags + (() if sti is not None else ("ASSUMED_ZERO:short_term_investments",))
    return _ok(debt.value - (cash + (sti or 0.0)), flags)


def debt_to_equity(debt: MetricResult, stockholders_equity: object) -> MetricResult:
    """Total debt / stockholders' equity (parent shareholders, excluding non-controlling interests).

    Zero equity -> ZERO_DENOMINATOR; negative equity -> NOT_MEANINGFUL (a negative ratio would read as "low leverage").
    """
    blocked = _depends_on(debt)
    if blocked:
        return blocked
    equity = _num(stockholders_equity)
    if equity is None:
        return _no(MetricState.MISSING_INPUT, "EQUITY_NOT_REPORTED")
    if equity == 0:
        return _no(MetricState.ZERO_DENOMINATOR, "ZERO_EQUITY")
    if equity < 0:
        return _no(MetricState.NOT_MEANINGFUL, "NEGATIVE_EQUITY")
    return _ok(debt.value / equity, debt.flags)


# --- valuation ------------------------------------------------------------------------------------------------------


def price_to_sales(market_cap: object, revenue_ttm: object) -> MetricResult:
    """Market capitalisation / trailing-twelve-month revenue."""
    cap, revenue = _num(market_cap), _num(revenue_ttm)
    if cap is None or cap <= 0:
        return _no(MetricState.MISSING_INPUT, "MARKET_CAP_MISSING")
    if revenue is None:
        return _no(MetricState.MISSING_INPUT, "REVENUE_MISSING")
    if revenue == 0:
        return _no(MetricState.ZERO_DENOMINATOR, "ZERO_REVENUE")
    if revenue < 0:
        return _no(MetricState.NOT_MEANINGFUL, "NEGATIVE_REVENUE")
    return _ok(cap / revenue)


def price_to_earnings(price: object, diluted_eps_ttm: object) -> MetricResult:
    """Price / trailing-twelve-month diluted EPS. Undefined for losses: a negative P/E is never produced."""
    px, eps = _num(price), _num(diluted_eps_ttm)
    if px is None or px <= 0:
        return _no(MetricState.MISSING_INPUT, "PRICE_MISSING")
    if eps is None:
        return _no(MetricState.MISSING_INPUT, "EPS_MISSING")
    if eps == 0:
        return _no(MetricState.ZERO_DENOMINATOR, "ZERO_EPS")
    if eps < 0:
        return _no(MetricState.NOT_MEANINGFUL, "NEGATIVE_EARNINGS")
    return _ok(px / eps)


# --- prices ---------------------------------------------------------------------------------------------------------


def price_return(close_now: object, close_then: object) -> MetricResult:
    """Price return over a horizon from split-adjusted closes; excludes dividends (source is not dividend-adjusted)."""
    now, then = _num(close_now), _num(close_then)
    if now is None or then is None or then <= 0:
        return _no(MetricState.MISSING_INPUT, "PRICE_MISSING")
    return _ok(now / then - 1, ("PRICE_RETURN_EXCLUDES_DIVIDENDS",))


def anchor_date(as_of: date, *, days: int = 0, months: int = 0, years: int = 0) -> date:
    """``as_of`` minus a calendar horizon (month-end clamped: 31 Mar - 1 month = 28/29 Feb)."""
    total_months = as_of.year * 12 + (as_of.month - 1) - months - years * 12
    year, month = divmod(total_months, 12)
    month += 1
    first_next = date(year + (month == 12), month % 12 + 1, 1)
    last_day = (first_next - date.resolution).day
    return date.fromordinal(date(year, month, min(as_of.day, last_day)).toordinal() - days)


def lookback_return(
    closes: Iterable[tuple[date, float | None]],
    as_of: date,
    *,
    days: int = 0,
    months: int = 0,
    years: int = 0,
    max_gap_days: int = 7,
) -> MetricResult:
    """Price return from the last close on/before ``as_of`` to the last close on/before ``as_of`` minus the horizon.

    Horizons are calendar-anchored (1W = 7 days, 1M = 1 calendar month, 1Y = 12 months), never "N trading days",
    so weekends, holidays and half-days cannot shift the base. If the series starts after the anchor date (recent IPO)
    or the nearest earlier close is more than ``max_gap_days`` before the anchor (data gap) the result is
    MISSING_INPUT rather than a shorter-horizon number presented as the requested one.
    """
    series = sorted((d, c) for d, c in closes if d <= as_of and _num(c) is not None)
    if not series:
        return _no(MetricState.MISSING_INPUT, "PRICE_MISSING")
    target = anchor_date(as_of, days=days, months=months, years=years)
    earlier = [(d, c) for d, c in series if d <= target]
    if not earlier:
        return _no(MetricState.MISSING_INPUT, "INSUFFICIENT_HISTORY")
    base_date, base_close = earlier[-1]
    if (target - base_date).days > max_gap_days:
        return _no(MetricState.MISSING_INPUT, "BASE_PRICE_STALE")
    return price_return(series[-1][1], base_close)


def drawdown_from_high(close: object, high_52w: object) -> MetricResult:
    """``close / 52-week high - 1`` (<= 0). A close above its own period high signals inconsistent data."""
    c, high = _num(close), _num(high_52w)
    if c is None or high is None or high <= 0 or c <= 0:
        return _no(MetricState.MISSING_INPUT, "PRICE_MISSING")
    if c > high * (1 + 1e-9):
        return _no(MetricState.NOT_MEANINGFUL, "CLOSE_ABOVE_HIGH")
    return _ok(c / high - 1)


def issuer_market_cap(caps_by_symbol: dict[str, object], primary_symbol: str) -> MetricResult:
    """One market cap per ISSUER from per-listing quotes.

    The audit (LIVE, 2026-10-02) found Nasdaq's ``market_cap`` is price x ALL shares of the issuer, computed per
    quoted class: GOOGL 4.137T and GOOG 4.096T describe the same company, BRK.A and BRK.B likewise. Summing the
    listings double counts, so the issuer value is the designated primary class's figure only, flagged when other
    classes exist. A missing primary figure is MISSING_INPUT, not a silent switch to another class.
    """
    value = _num(caps_by_symbol.get(primary_symbol))
    if value is None or value <= 0:
        return _no(MetricState.MISSING_INPUT, "PRIMARY_CLASS_MARKET_CAP_MISSING")
    others = [k for k in caps_by_symbol if k != primary_symbol]
    return _ok(value, ("MULTI_CLASS_ALL_SHARES_AT_PRIMARY_PRICE",) if others else ())


def consistent_report_period(report_dates: Iterable[date | None]) -> MetricResult:
    """Do holder rows share one reporting period? Mixed periods must not be summed into one 'ownership' number.

    Nasdaq's institutional list for AAPL mixed 2025-12-31 (Vanguard Group Inc) with 2026-06-30 rows (LIVE).
    """
    dates = list(report_dates)
    if not dates or any(d is None for d in dates):
        return _no(MetricState.MISSING_INPUT, "REPORT_DATE_MISSING")
    if len(set(dates)) > 1:
        return _no(MetricState.NOT_COMPARABLE, "MIXED_REPORT_PERIODS")
    return _ok(1.0)


def high_low_52w(
    rows: Iterable[tuple[date, float | None, float | None, float | None]],
    as_of: date,
    *,
    minimum_days: int = 20,
    full_window_days: int = 240,
) -> dict[str, MetricResult]:
    """52-week intraday high/low and the close-based equivalents, from daily (date, high, low, close) rows.

    The window is the 365 calendar days ending ``as_of`` (inclusive), which is how Nasdaq's published year high/low
    behaved in the audit (max high / min low). Rows after ``as_of`` are ignored (point-in-time safe). Fewer than
    ``minimum_days`` trading days -> MISSING_INPUT; fewer than ``full_window_days`` -> OK with PARTIAL_WINDOW.
    """
    start = date.fromordinal(as_of.toordinal() - 365)
    window = [r for r in rows if start <= r[0] <= as_of]
    highs = [h for _, h, _, _ in window if _num(h) is not None]
    lows = [lo for _, _, lo, _ in window if _num(lo) is not None]
    closes = [c for _, _, _, c in window if _num(c) is not None]
    out: dict[str, MetricResult] = {}
    for name, values, pick in (
        ("high_52w", highs, max),
        ("low_52w", lows, min),
        ("high_52w_close", closes, max),
        ("low_52w_close", closes, min),
    ):
        if len(values) < minimum_days:
            out[name] = _no(MetricState.MISSING_INPUT, "INSUFFICIENT_HISTORY")
        else:
            out[name] = _ok(float(pick(values)), () if len(values) >= full_window_days else ("PARTIAL_WINDOW",))
    return out


# --- periods and comparability --------------------------------------------------------------------------------------


class PeriodKind(StrEnum):
    INSTANT = "INSTANT"  # balance-sheet date
    QUARTER = "QUARTER"  # ~12-14 weeks
    HALF_YEAR_YTD = "HALF_YEAR_YTD"
    NINE_MONTH_YTD = "NINE_MONTH_YTD"
    FISCAL_YEAR = "FISCAL_YEAR"  # 350-380 days: covers 52/53-week years
    OTHER = "OTHER"  # transition periods, short/long stubs


def period_kind(period_start: date | None, period_end: date) -> PeriodKind:
    """Classify a reporting period by its length. Duration facts only; ``period_start=None`` means an instant."""
    if period_start is None:
        return PeriodKind.INSTANT
    days = (period_end - period_start).days + 1
    if 84 <= days <= 98:
        return PeriodKind.QUARTER
    if 170 <= days <= 195:
        return PeriodKind.HALF_YEAR_YTD
    if 260 <= days <= 285:
        return PeriodKind.NINE_MONTH_YTD
    if 350 <= days <= 380:
        return PeriodKind.FISCAL_YEAR
    return PeriodKind.OTHER


def comparable_year_over_year(now: tuple[date | None, date], prior: tuple[date | None, date]) -> MetricResult:
    """May these two periods be compared year over year? Same kind, a real one, and ends ~one year apart.

    A fiscal-year change (transition period), a stub period or a 53-week quirk beyond tolerance -> NOT_COMPARABLE.
    """
    kind_now, kind_prior = period_kind(*now), period_kind(*prior)
    if kind_now is PeriodKind.OTHER or kind_now != kind_prior:
        return _no(MetricState.NOT_COMPARABLE, "PERIOD_LENGTH_MISMATCH")
    if not 350 <= (now[1] - prior[1]).days <= 380:
        return _no(MetricState.NOT_COMPARABLE, "NOT_ONE_YEAR_APART")
    return _ok(1.0)


# --- restatements and point-in-time ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class FactPoint:
    """One as-reported value of one concept for one period, from one filing."""

    value: float
    period_start: date | None
    period_end: date
    filed: date  # the filing's acceptance date; a value is KNOWN from this date on
    accession: str
    form: str | None = None


def latest_known(points: Sequence[FactPoint], *, as_of: date | None = None) -> FactPoint | None:
    """The value a reader would have used: the most recently filed point (ties: highest accession number).

    ``as_of=None`` gives the current view (restatements and amendments included). With ``as_of`` it is point-in-time:
    filings made after that date do not exist yet. An amendment (10-K/A) wins over its original only because it is filed
    later AND carries that period's value. Pass the points of ONE concept and ONE period.
    """
    known = [p for p in points if as_of is None or p.filed <= as_of]
    return max(known, key=lambda p: (p.filed, p.accession)) if known else None


def same_filing_pair(
    points: Sequence[FactPoint],
    period_end_now: date,
    period_end_prior: date,
    *,
    as_of: date | None = None,
) -> tuple[FactPoint, FactPoint] | None:
    """The (current, prior) values of ONE concept taken from ONE filing, so both are on the same basis.

    Per-share figures and restated amounts change between filings (the audit saw NVDA's FY2024 diluted EPS as 1.19
    in one vintage and 11.93 in another around its 10-for-1 split, next to an FY2023 figure still on the old basis).
    A year-over-year comparison is only valid when both numbers come from the same accession; the filing used is the
    most recently filed one (not after ``as_of``) that reports BOTH periods. Returns None if no filing has both.
    ``points`` are all vintages of one concept and one duration length (e.g. fiscal-year EPS).
    """
    by_accession: dict[str, dict[date, FactPoint]] = {}
    for point in points:
        if as_of is None or point.filed <= as_of:
            by_accession.setdefault(point.accession, {})[point.period_end] = point
    candidates = [
        (periods[period_end_now].filed, accession, periods[period_end_now], periods[period_end_prior])
        for accession, periods in by_accession.items()
        if period_end_now in periods and period_end_prior in periods
    ]
    if not candidates:
        return None
    _, _, now, prior = max(candidates, key=lambda c: (c[0], c[1]))
    return now, prior


def was_restated(points: Sequence[FactPoint]) -> bool:
    """True if different filings reported different values for the same period."""
    return len({p.value for p in points}) > 1


# --- insider transactions -------------------------------------------------------------------------------------------


class Form4Class(StrEnum):
    OPEN_MARKET_BUY = "OPEN_MARKET_BUY"
    OPEN_MARKET_SELL = "OPEN_MARKET_SELL"
    OPTION_EXERCISE_OR_CONVERSION = "OPTION_EXERCISE_OR_CONVERSION"
    AWARD = "AWARD"
    TAX_WITHHOLDING = "TAX_WITHHOLDING"
    GIFT = "GIFT"
    DISPOSITION_TO_ISSUER = "DISPOSITION_TO_ISSUER"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    EXPIRATION_OR_CANCELLATION = "EXPIRATION_OR_CANCELLATION"
    DISCRETIONARY_PLAN_TRADE = "DISCRETIONARY_PLAN_TRADE"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


_FORM4_CODES = {
    "P": Form4Class.OPEN_MARKET_BUY,
    "S": Form4Class.OPEN_MARKET_SELL,
    "M": Form4Class.OPTION_EXERCISE_OR_CONVERSION,
    "X": Form4Class.OPTION_EXERCISE_OR_CONVERSION,
    "C": Form4Class.OPTION_EXERCISE_OR_CONVERSION,
    "O": Form4Class.OPTION_EXERCISE_OR_CONVERSION,
    "A": Form4Class.AWARD,
    "F": Form4Class.TAX_WITHHOLDING,
    "G": Form4Class.GIFT,
    "D": Form4Class.DISPOSITION_TO_ISSUER,
    "U": Form4Class.CORPORATE_ACTION,
    "E": Form4Class.EXPIRATION_OR_CANCELLATION,
    "H": Form4Class.EXPIRATION_OR_CANCELLATION,
    "I": Form4Class.DISCRETIONARY_PLAN_TRADE,
    "J": Form4Class.OTHER,
    "K": Form4Class.OTHER,
    "L": Form4Class.OTHER,
    "V": Form4Class.OTHER,
    "W": Form4Class.OTHER,
    "Z": Form4Class.OTHER,
}


def classify_form4(transaction_code: str | None) -> Form4Class:
    """Map an SEC Form 4 transaction code (``transactionCode``) to an economic class. Unknown/absent -> UNKNOWN."""
    if not transaction_code:
        return Form4Class.UNKNOWN
    return _FORM4_CODES.get(transaction_code.strip().upper(), Form4Class.UNKNOWN)


_FORM4_DESCRIPTIONS = {
    "Grant, award or other acquisition pursuant to Rule 16b-3(d)": "A",
    "Conversion of derivative security": "C",
    "Disposition to the issuer of issuer equity securities pursuant to Rule 16b-3(e)": "D",
    "Expiration of short derivative position": "E",
    "Payment of exercise price or tax liability by delivering or withholding securities incident to the receipt, "
    "exercise or vesting of a security issued in accordance with Rule 16b-3": "F",
    "Bona fide gift": "G",
    "Expiration (or cancellation) of long derivative position with value received": "H",
    "Discretionary transaction in accordance with Rule 16b-3(f) "
    "resulting in acquisition or disposition of issuer securities": "I",
    "Other acquisition or disposition (describe transaction)": "J",
    "Small acquisition under Rule 16a-6": "L",
    "Exercise or conversion of derivative security exempted pursuant to Rule 16b-3": "M",
    "Exercise of out-of-the-money derivative security": "O",
    "Open market or private purchase of non-derivative or derivative security": "P",
    "Open market or private sale of non-derivative or derivative security": "S",
    "Disposition pursuant to a tender of shares in a change of control transaction": "U",
    "Acquisition or disposition by will or the laws of descent and distribution": "W",
    "Exercise of in-the-money or at-the-money derivative security": "X",
    "Deposit into or withdrawal from voting trust": "Z",
}


def classify_form4_description(description: str | None) -> Form4Class:
    """Classify the text OpenBB's ``obb.sec.insider_trading`` returns in ``transaction_type``.

    OpenBB replaces the one-letter SEC transaction code with its description, so the letter is recovered by exact
    lookup of the SEC's own wording (audit: LIVE, AAPL/NVDA). Anything not in the table -> UNKNOWN (never guessed).
    Nasdaq's insider feed labels pre-arranged plan sales "Automatic Sell"; that is the only Nasdaq label observed.
    """
    if not description:
        return Form4Class.UNKNOWN
    text = description.strip()
    if text == "Automatic Sell":
        return Form4Class.OPEN_MARKET_SELL
    return classify_form4(_FORM4_DESCRIPTIONS.get(text))


def is_market_signal(kind: Form4Class) -> bool:
    """Only open-market purchases and sales reflect a discretionary decision to trade at market prices.
    Awards, tax withholding, exercises, gifts and corporate actions are compensation mechanics or non-market events."""
    return kind in (Form4Class.OPEN_MARKET_BUY, Form4Class.OPEN_MARKET_SELL)
