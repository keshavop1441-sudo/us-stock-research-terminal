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
) -> MetricResult:
    """Financial debt = short-term borrowings + current portion of long-term debt + long-term debt + finance leases.

    Operating-lease liabilities are excluded (use a separate, explicitly named metric if wanted).
    XBRL filers omit zero lines, so an absent COMPONENT counts as zero WITH A FLAG, but only when the balance sheet
    itself is present for that period; with no balance sheet there is no evidence either way -> MISSING_INPUT.
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
    if not present:
        return _ok(0.0, ("NO_DEBT_LINES_REPORTED_ASSUMED_ZERO",))
    absent = sorted(k for k, v in parts.items() if v is None and k != "finance_lease_liabilities")
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


def drawdown_from_high(close: object, high_52w: object) -> MetricResult:
    """``close / 52-week high - 1`` (<= 0). A close above its own period high signals inconsistent data."""
    c, high = _num(close), _num(high_52w)
    if c is None or high is None or high <= 0 or c <= 0:
        return _no(MetricState.MISSING_INPUT, "PRICE_MISSING")
    if c > high * (1 + 1e-9):
        return _no(MetricState.NOT_MEANINGFUL, "CLOSE_ABOVE_HIGH")
    return _ok(c / high - 1)


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


def is_market_signal(kind: Form4Class) -> bool:
    """Only open-market purchases and sales reflect a discretionary decision to trade at market prices.
    Awards, tax withholding, exercises, gifts and corporate actions are compensation mechanics or non-market events."""
    return kind in (Form4Class.OPEN_MARKET_BUY, Form4Class.OPEN_MARKET_SELL)
