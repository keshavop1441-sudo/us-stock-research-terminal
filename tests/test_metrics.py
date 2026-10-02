"""The metric dictionary's N/A semantics, executable (docs/data_coverage.yaml, app/screening/metrics.py)."""

import math
from datetime import date

import pytest

from app.screening import metrics as m
from app.screening.metrics import Form4Class, MetricResult, MetricState

OK, MISSING, ZERO, NOT_MEANINGFUL, NOT_COMPARABLE = (
    MetricState.OK,
    MetricState.MISSING_INPUT,
    MetricState.ZERO_DENOMINATOR,
    MetricState.NOT_MEANINGFUL,
    MetricState.NOT_COMPARABLE,
)


def ok(value, flags=()):
    return MetricResult(OK, value, None, tuple(flags))


# --- growth: the cases that must NOT produce a percentage -----------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "prior", "state", "reason"),
    [
        (2.0, -1.0, NOT_MEANINGFUL, "SIGN_CHANGE"),  # negative EPS -> positive EPS
        (0.5, -0.01, NOT_MEANINGFUL, "SIGN_CHANGE"),  # tiny negative base -> positive: still not a growth rate
        (-2.0, -1.0, NOT_MEANINGFUL, "NEGATIVE_BASE"),  # loss gets bigger
        (-0.5, -1.0, NOT_MEANINGFUL, "NEGATIVE_BASE"),  # loss narrows: an improvement, but not "growth"
        (0.0, -1.0, NOT_MEANINGFUL, "NEGATIVE_BASE"),
        (2.0, 0.0, ZERO, "ZERO_BASE"),  # zero prior -> positive
        (0.0, 0.0, ZERO, "ZERO_BASE"),
        (-1.0, 2.0, NOT_MEANINGFUL, "SIGN_CHANGE"),  # profit -> loss is not "-150%"
        (None, 1.0, MISSING, "MISSING_VALUE"),
        (1.0, None, MISSING, "MISSING_VALUE"),
        (float("nan"), 1.0, MISSING, "MISSING_VALUE"),
        (1.0, float("inf"), MISSING, "MISSING_VALUE"),
        ("n/a", 1.0, MISSING, "MISSING_VALUE"),
        (True, 1.0, MISSING, "MISSING_VALUE"),  # a bool is not a quantity
    ],
)
def test_growth_rate_refuses_meaningless_percentages(current, prior, state, reason):
    result = m.growth_rate(current, prior)
    assert (result.state, result.reason, result.value) == (state, reason, None)
    assert not result.ok


@pytest.mark.parametrize(
    ("current", "prior", "expected"),
    [(120, 100, 0.2), (100, 100, 0.0), (50, 100, -0.5), (0, 100, -1.0), (1.91, 1.85, 1.91 / 1.85 - 1)],
)
def test_growth_rate_for_positive_values(current, prior, expected):
    result = m.growth_rate(current, prior)
    assert result.ok and result.value == pytest.approx(expected)


# --- margins ----------------------------------------------------------------------------------------------------------


def test_margins_including_negative_margins():
    assert m.margin(40, 100).value == pytest.approx(0.4)
    assert m.margin(-25, 100).value == pytest.approx(-0.25)  # a loss margin is a valid value
    assert m.margin(10, 0).state is ZERO
    assert m.margin(10, -5) == MetricResult(NOT_MEANINGFUL, None, "NEGATIVE_REVENUE")
    assert m.margin(None, 100).state is MISSING and m.margin(10, None).state is MISSING


def test_gross_profit_is_derived_only_from_the_cost_of_revenue_line():
    derived = m.gross_profit_from_components(100, 60)
    assert derived.value == 40 and derived.flags == ("DERIVED_GROSS_PROFIT",)
    assert m.gross_profit_from_components(100, None).reason == "COST_OF_REVENUE_NOT_REPORTED"


def test_margin_change_is_a_level_difference_and_works_across_negative_margins():
    assert m.margin_change(m.margin(30, 100), m.margin(20, 100)).value == pytest.approx(0.10)  # improving
    assert m.margin_change(m.margin(-10, 100), m.margin(-30, 100)).value == pytest.approx(0.20)  # loss shrinking
    blocked = m.margin_change(m.margin(30, 100), m.margin(5, 0))
    assert blocked.state is ZERO and blocked.reason == "DEPENDS_ON_UNDEFINED_INPUT"
    assert m.margin_change(m.margin(30, 100), m.margin(None, 100)).state is MISSING


# --- free cash flow ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("capex", [30, -30])  # SEC reports payments as positive; OpenBB statements as negative
def test_free_cash_flow_uses_the_magnitude_of_capex(capex):
    assert m.free_cash_flow(100, capex).value == 70


def test_free_cash_flow_never_assumes_zero_capex():
    assert m.free_cash_flow(100, None) == MetricResult(MISSING, None, "CAPEX_NOT_REPORTED")
    assert m.free_cash_flow(None, 30).reason == "OPERATING_CASH_FLOW_MISSING"
    assert (
        m.free_cash_flow(-50, 30).value == -80
    )  # negative FCF is a valid value; FCF *growth* then follows growth_rate


# --- debt, net debt, leverage -----------------------------------------------------------------------------------------


def test_total_debt_sums_components_and_flags_absent_ones():
    full = m.total_debt(10, 5, 85, balance_sheet_present=True)
    assert full.value == 100 and full.flags == ()
    partial = m.total_debt(None, 5, 85, balance_sheet_present=True)
    assert partial.value == 90 and partial.flags == ("ASSUMED_ZERO:short_term_debt",)
    assert m.total_debt(10, 5, 85, 7, balance_sheet_present=True).value == 107  # finance leases included


def test_total_debt_without_any_debt_line_is_zero_only_with_a_balance_sheet_and_a_flag():
    debt_free = m.total_debt(None, None, None, balance_sheet_present=True)
    assert debt_free.ok and debt_free.value == 0 and debt_free.flags == ("NO_DEBT_LINES_REPORTED_ASSUMED_ZERO",)
    assert m.total_debt(None, None, None, balance_sheet_present=False) == MetricResult(
        MISSING, None, "NO_BALANCE_SHEET"
    )
    assert m.total_debt(-1, None, 5, balance_sheet_present=True).reason == "NEGATIVE_DEBT_COMPONENT"


def test_net_debt_and_net_cash():
    debt = m.total_debt(0, 10, 90, balance_sheet_present=True)
    assert m.net_debt(debt, 30, 20).value == 50
    assert m.net_debt(debt, 150, 0).value == -50  # net cash is negative net debt
    assert m.net_debt(debt, None, 20).reason == "CASH_NOT_REPORTED"
    no_sti = m.net_debt(debt, 30, None)
    assert no_sti.value == 70 and "ASSUMED_ZERO:short_term_investments" in no_sti.flags
    assert m.net_debt(m.total_debt(None, None, None, balance_sheet_present=False), 30, 20).state is MISSING


def test_debt_to_equity_rules():
    debt = m.total_debt(0, 0, 100, balance_sheet_present=True)
    assert m.debt_to_equity(debt, 50).value == 2.0
    assert m.debt_to_equity(debt, 0).state is ZERO
    assert m.debt_to_equity(debt, -20) == MetricResult(
        NOT_MEANINGFUL, None, "NEGATIVE_EQUITY"
    )  # never a negative ratio
    assert m.debt_to_equity(debt, None).state is MISSING
    assert m.debt_to_equity(m.total_debt(None, None, None, balance_sheet_present=False), 50).state is MISSING


def test_leverage_trend_is_a_difference_of_two_defined_ratios():
    now = m.debt_to_equity(m.total_debt(0, 0, 80, balance_sheet_present=True), 100)
    before = m.debt_to_equity(m.total_debt(0, 0, 100, balance_sheet_present=True), 100)
    change = m.margin_change(now, before)  # same level-difference rule
    assert change.value == pytest.approx(-0.2)  # leverage fell: no deterioration


# --- valuation --------------------------------------------------------------------------------------------------------


def test_price_to_sales():
    assert m.price_to_sales(500, 100).value == 5.0
    assert m.price_to_sales(500, 0).state is ZERO
    assert m.price_to_sales(500, -1).reason == "NEGATIVE_REVENUE"
    assert m.price_to_sales(None, 100).reason == "MARKET_CAP_MISSING"
    assert m.price_to_sales(0, 100).reason == "MARKET_CAP_MISSING"
    assert m.price_to_sales(500, None).reason == "REVENUE_MISSING"


def test_price_to_earnings_is_never_negative():
    assert m.price_to_earnings(100, 5).value == 20.0
    assert m.price_to_earnings(100, -0.64) == MetricResult(NOT_MEANINGFUL, None, "NEGATIVE_EARNINGS")
    assert m.price_to_earnings(100, 0).state is ZERO
    assert m.price_to_earnings(100, None).reason == "EPS_MISSING"
    assert m.price_to_earnings(None, 5).reason == "PRICE_MISSING"


# --- prices -----------------------------------------------------------------------------------------------------------


def test_price_return_and_drawdown():
    r = m.price_return(110, 100)
    assert r.value == pytest.approx(0.10) and "PRICE_RETURN_EXCLUDES_DIVIDENDS" in r.flags
    assert m.price_return(110, 0).state is MISSING and m.price_return(None, 100).state is MISSING
    assert m.drawdown_from_high(70, 100).value == pytest.approx(-0.30)
    assert m.drawdown_from_high(100, 100).value == 0.0
    assert m.drawdown_from_high(101, 100).reason == "CLOSE_ABOVE_HIGH"
    assert m.drawdown_from_high(70, None).state is MISSING


def rows(n, start=date(2025, 10, 2)):
    from datetime import timedelta

    return [(start + timedelta(days=i), 100 + i, 90 + i, 95 + i) for i in range(n)]


def test_52_week_high_low_use_intraday_values_and_a_365_day_window():
    data = rows(250)
    result = m.high_low_52w(data, as_of=date(2026, 6, 8))
    in_window = [high for day, high, _low, _close in data if date(2025, 6, 8) <= day <= date(2026, 6, 8)]
    assert result["high_52w"].value == max(in_window)
    assert result["high_52w"].value != result["high_52w_close"].value  # intraday high differs from the close-based high
    assert result["low_52w"].value == 90.0
    assert result["high_52w"].ok and result["high_52w"].flags == ()


def test_52_week_values_ignore_future_rows_and_flag_short_history():
    data = rows(30)
    early = m.high_low_52w(data, as_of=date(2025, 10, 20))  # only the first 19 rows are known on that date
    assert early["high_52w"].state is MISSING and early["high_52w"].reason == "INSUFFICIENT_HISTORY"
    partial = m.high_low_52w(data, as_of=date(2025, 11, 5))
    assert partial["high_52w"].ok and partial["high_52w"].flags == ("PARTIAL_WINDOW",)
    assert partial["high_52w"].value == 100 + 29  # rows are monotonically rising; the high is the last known one


# --- periods and comparability ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "kind"),
    [
        (date(2023, 10, 1), date(2024, 9, 28), m.PeriodKind.FISCAL_YEAR),  # Apple: 52-week year
        (date(2024, 2, 1), date(2025, 1, 26), m.PeriodKind.FISCAL_YEAR),  # NVIDIA-style year ending late January
        (date(2022, 8, 29), date(2023, 9, 3), m.PeriodKind.FISCAL_YEAR),  # a 53-week year (371 days)
        (date(2024, 4, 1), date(2024, 6, 30), m.PeriodKind.QUARTER),
        (date(2024, 4, 1), date(2024, 9, 28), m.PeriodKind.HALF_YEAR_YTD),
        (date(2024, 1, 1), date(2024, 9, 28), m.PeriodKind.NINE_MONTH_YTD),
        (date(2024, 7, 1), date(2024, 12, 31), m.PeriodKind.HALF_YEAR_YTD),
        (date(2023, 1, 1), date(2023, 5, 31), m.PeriodKind.OTHER),  # transition-period stub
        (None, date(2024, 9, 28), m.PeriodKind.INSTANT),
    ],
)
def test_period_kind(start, end, kind):
    assert m.period_kind(start, end) is kind


def test_year_over_year_comparability():
    fy24 = (date(2023, 10, 1), date(2024, 9, 28))
    fy23 = (date(2022, 9, 25), date(2023, 9, 30))
    assert m.comparable_year_over_year(fy24, fy23).ok
    q = (date(2024, 4, 1), date(2024, 6, 30))
    assert m.comparable_year_over_year(q, (date(2023, 4, 2), date(2023, 7, 1))).ok
    assert m.comparable_year_over_year(q, fy23) == MetricResult(NOT_COMPARABLE, None, "PERIOD_LENGTH_MISMATCH")
    transition = (date(2024, 1, 1), date(2024, 5, 31))  # fiscal-year change: a 5-month stub
    assert m.comparable_year_over_year(transition, fy23).state is NOT_COMPARABLE
    assert m.comparable_year_over_year(transition, transition).reason == "PERIOD_LENGTH_MISMATCH"
    two_years = (date(2021, 10, 1), date(2022, 9, 24))
    assert m.comparable_year_over_year(fy24, two_years).reason == "NOT_ONE_YEAR_APART"


# --- restatements and point-in-time -----------------------------------------------------------------------------------


def fact(value, filed, accession, form="10-K"):
    return m.FactPoint(value, date(2023, 1, 1), date(2023, 12, 31), filed, accession, form)


ORIGINAL = fact(100.0, date(2024, 2, 1), "0000000001-24-000010")
AMENDED = fact(95.0, date(2024, 6, 1), "0000000001-24-000050", "10-K/A")
RESTATED_IN_NEXT_YEAR = fact(97.0, date(2025, 2, 1), "0000000001-25-000012")


def test_current_view_uses_the_latest_filed_value():
    assert m.latest_known([ORIGINAL, AMENDED, RESTATED_IN_NEXT_YEAR]).value == 97.0
    assert m.latest_known([RESTATED_IN_NEXT_YEAR, ORIGINAL, AMENDED]).value == 97.0  # order of input is irrelevant


def test_point_in_time_view_only_knows_filings_made_by_the_as_of_date():
    points = [ORIGINAL, AMENDED, RESTATED_IN_NEXT_YEAR]
    assert m.latest_known(points, as_of=date(2024, 1, 31)) is None  # nothing was public yet
    assert m.latest_known(points, as_of=date(2024, 2, 1)).value == 100.0  # the original, on its filing day
    assert m.latest_known(points, as_of=date(2024, 5, 31)).value == 100.0  # the amendment is not known yet
    assert m.latest_known(points, as_of=date(2024, 6, 1)).value == 95.0
    assert m.latest_known(points, as_of=date(2025, 1, 1)).value == 95.0


def test_same_day_filings_resolve_deterministically_by_accession():
    a, b = fact(1.0, date(2024, 2, 1), "0000000001-24-000010"), fact(2.0, date(2024, 2, 1), "0000000001-24-000011")
    assert m.latest_known([a, b]).value == 2.0 and m.latest_known([b, a]).value == 2.0


def test_restatement_detection():
    assert m.was_restated([ORIGINAL, AMENDED])
    assert not m.was_restated(
        [ORIGINAL, fact(100.0, date(2025, 2, 1), "0000000001-25-000012")]
    )  # re-reported unchanged
    assert not m.was_restated([ORIGINAL])


# --- Form 4 classification --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("P", Form4Class.OPEN_MARKET_BUY),
        ("S", Form4Class.OPEN_MARKET_SELL),
        ("s", Form4Class.OPEN_MARKET_SELL),
        (" S ", Form4Class.OPEN_MARKET_SELL),
        ("A", Form4Class.AWARD),
        ("F", Form4Class.TAX_WITHHOLDING),
        ("G", Form4Class.GIFT),
        ("M", Form4Class.OPTION_EXERCISE_OR_CONVERSION),
        ("X", Form4Class.OPTION_EXERCISE_OR_CONVERSION),
        ("C", Form4Class.OPTION_EXERCISE_OR_CONVERSION),
        ("D", Form4Class.DISPOSITION_TO_ISSUER),
        ("U", Form4Class.CORPORATE_ACTION),
        ("E", Form4Class.EXPIRATION_OR_CANCELLATION),
        ("I", Form4Class.DISCRETIONARY_PLAN_TRADE),
        ("J", Form4Class.OTHER),
        ("Q", Form4Class.UNKNOWN),
        ("", Form4Class.UNKNOWN),
        (None, Form4Class.UNKNOWN),
    ],
)
def test_form4_codes(code, kind):
    assert m.classify_form4(code) is kind


def test_only_open_market_buys_and_sells_are_market_signals():
    signals = {k for k in Form4Class if m.is_market_signal(k)}
    assert signals == {Form4Class.OPEN_MARKET_BUY, Form4Class.OPEN_MARKET_SELL}


def test_every_sec_transaction_code_documented_by_openbb_is_classified():
    """OpenBB's installed Form 4 parser documents these codes; none may fall through to UNKNOWN."""
    from openbb_sec.utils.form4 import transaction_code_map

    unclassified = [c for c in transaction_code_map if m.classify_form4(c) is Form4Class.UNKNOWN]
    assert unclassified == []


def test_results_are_immutable_and_never_nan():
    result = m.growth_rate(2, 1)
    with pytest.raises(AttributeError):
        result.value = 5
    assert not math.isnan(result.value)
