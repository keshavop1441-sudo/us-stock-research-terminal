"""Margin-change and leverage-change metrics (screening inputs "improving margins", "leverage not worsening").

Built on hand-made SEC facts so every number is explicit. Rules under test: both years from ONE filing; a restated
input from another filing is NOT_COMPARABLE; absent debt lines are never zero; negative equity is not meaningful.
"""

from datetime import date

from app.screening import metrics as m
from app.screening.fundamentals import Fact, StatementIndex
from app.screening.metrics import MetricState
from app.screening.snapshot import _leverage_change, _margin_change

D = date
FY, PRIOR = D(2025, 9, 27), D(2024, 9, 28)
FILED = D(2025, 11, 1)
K1, K2 = "0000000001-25-000100", "0000000001-25-000200"  # two filings (e.g. 10-K and a 10-K/A)


def duration(tag, now, prior, accn=K1, filed=FILED, accn_prior=None):
    unit = "USD"
    return [
        Fact("us-gaap", tag, unit, now, D(2024, 9, 29), FY, filed, accn, "10-K"),
        Fact("us-gaap", tag, unit, prior, D(2023, 10, 1), PRIOR, filed, accn_prior or accn, "10-K"),
    ]


def instant(tag, now, prior, accn=K1, filed=FILED):
    return [
        Fact("us-gaap", tag, "USD", now, None, FY, filed, accn, "10-K"),
        Fact("us-gaap", tag, "USD", prior, None, PRIOR, filed, accn, "10-K"),
    ]


def facts(*groups):
    return StatementIndex([f for g in groups for f in g])


def test_margin_change_is_the_difference_of_two_margins_from_one_filing():
    index = facts(duration("Revenues", 200.0, 100.0), duration("GrossProfit", 100.0, 40.0))
    result = _margin_change(index, "gross_profit", FY)
    assert result.state is MetricState.OK and abs(result.value - (0.5 - 0.4)) < 1e-12


def test_margin_change_works_for_negative_margins_as_a_level_difference():
    index = facts(duration("Revenues", 100.0, 100.0), duration("OperatingIncomeLoss", -10.0, -30.0))
    assert abs(_margin_change(index, "operating_income", FY).value - 0.2) < 1e-12  # loss narrowed: improving


def test_margin_change_refuses_inputs_from_different_filings():
    index = facts(
        duration("Revenues", 200.0, 100.0, accn=K2, filed=D(2026, 1, 5), accn_prior=K2),
        duration("GrossProfit", 100.0, 40.0, accn=K1),
    )
    result = _margin_change(index, "gross_profit", FY)
    assert result.state is MetricState.NOT_COMPARABLE and result.reason == "MARGIN_INPUTS_FROM_DIFFERENT_FILINGS"


def test_margin_change_is_missing_when_a_line_or_prior_year_is_absent():
    only_now = facts(duration("Revenues", 200.0, 100.0), [duration("GrossProfit", 100.0, 40.0)[0]])
    assert _margin_change(only_now, "gross_profit", FY).state is MetricState.MISSING_INPUT
    assert (
        _margin_change(facts(duration("Revenues", 200.0, 100.0)), "gross_profit", FY).state is MetricState.MISSING_INPUT
    )


def test_margin_change_with_zero_prior_revenue_is_not_a_number():
    index = facts(duration("Revenues", 200.0, 0.0), duration("GrossProfit", 100.0, 0.0))
    assert not _margin_change(index, "gross_profit", FY).ok


def debt_facts(
    debt_now,
    debt_prior,
    equity_now=100.0,
    equity_prior=100.0,
    accn=K1,
    lines=("ShortTermBorrowings", "LongTermDebtCurrent", "LongTermDebtNoncurrent"),
):
    out = [duration("Revenues", 200.0, 100.0, accn=accn)]
    for tag in lines:
        out.append(instant(tag, debt_now / 3, debt_prior / 3, accn=accn))
    out.append(instant("StockholdersEquity", equity_now, equity_prior, accn=accn))
    return facts(*out)


def test_leverage_change_is_positive_when_leverage_rose():
    out = _leverage_change(debt_facts(150.0, 90.0), FY)
    assert abs(out["debt_to_equity_fy"].value - 1.5) < 1e-9 and abs(out["debt_to_equity_prior_fy"].value - 0.9) < 1e-9
    assert abs(out["debt_to_equity_change_yoy"].value - 0.6) < 1e-9


def test_leverage_change_is_not_produced_when_any_core_debt_line_is_absent():
    """Absent is never zero: with the long-term line missing, total debt (and so the ratio) is MISSING_INPUT."""
    out = _leverage_change(debt_facts(150.0, 90.0, lines=("ShortTermBorrowings", "LongTermDebtCurrent")), FY)
    assert out["debt_to_equity_fy"].state is MetricState.MISSING_INPUT
    assert out["debt_to_equity_fy"].reason.startswith("DEPENDS") or "DEBT_COMPONENT_ABSENT" in str(
        out["debt_to_equity_fy"].reason
    )
    assert out["debt_to_equity_change_yoy"].state is MetricState.MISSING_INPUT


def test_leverage_change_is_blocked_by_negative_equity_in_either_year():
    out = _leverage_change(debt_facts(150.0, 90.0, equity_prior=-5.0), FY)
    assert out["debt_to_equity_prior_fy"].state is MetricState.NOT_MEANINGFUL
    assert out["debt_to_equity_change_yoy"].state is MetricState.NOT_MEANINGFUL


def test_leverage_uses_only_values_the_annual_filing_itself_reports():
    """A later filing (another accession) with different balance-sheet values must not leak into the pair."""
    base = debt_facts(150.0, 90.0)
    other = [Fact("us-gaap", "StockholdersEquity", "USD", 1.0, None, FY, D(2026, 1, 1), K2, "10-Q")]
    out = _leverage_change(StatementIndex([*base.facts, *other]), FY)  # type: ignore[attr-defined]
    assert abs(out["debt_to_equity_fy"].value - 1.5) < 1e-9


def test_leverage_change_metric_function_requires_both_ratios():
    ok = m.MetricResult(MetricState.OK, 1.0)
    assert m.leverage_change(ok, m.MetricResult(MetricState.OK, 0.4)).value == 0.6
    assert (
        m.leverage_change(ok, m.MetricResult(MetricState.MISSING_INPUT, None, "X")).state is MetricState.MISSING_INPUT
    )
    assert (
        m.leverage_change(m.MetricResult(MetricState.NOT_MEANINGFUL, None, "NEGATIVE_EQUITY"), ok).state
        is MetricState.NOT_MEANINGFUL
    )
