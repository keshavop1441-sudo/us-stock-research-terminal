"""Tag selection, period handling, TTM and metric states on SYNTHETIC SEC-shaped facts (tests/p0_synthetic.py).

What is proven here is the logic, not any provider's behaviour. Phase 2 rules exercised: tag per period with conflict
detection, same-filing growth, TTM alignment, the split guard, 'absent is not zero' for debt and capex, negative-EPS
P/E, amendments and point-in-time selection.
"""

from datetime import date

import p0_synthetic as syn
import pytest

from app.ingestion.sec_normalize import parse_companyfacts
from app.screening import metrics as m
from app.screening.fundamentals import FY_KINDS, Fact, StatementIndex
from app.screening.metrics import MetricState
from app.screening.snapshot import fundamental_metrics

D = date
CIK = "0000320193"
AS_OF = D(2026, 2, 15)


def facts_of(doc: dict) -> list[Fact]:
    parsed = parse_companyfacts(doc, CIK)
    assert not parsed.rejects
    return [
        Fact(p.taxonomy, p.concept, p.unit, p.value, p.period_start, p.period_end, p.filed, p.accession, p.form,
             p.fiscal_year, p.fiscal_period, p.frame)
        for p in parsed.points
    ]  # fmt: skip


def metrics_of(**kwargs):
    index = StatementIndex(facts_of(syn.standard_issuer(**kwargs)))
    out, lines = fundamental_metrics(index, AS_OF)
    return out, lines, index


def test_a_healthy_non_calendar_issuer_gets_every_core_metric():
    out, lines, _ = metrics_of()
    assert out["revenue_growth_yoy"].value == pytest.approx(0.10)
    assert out["gross_margin"].value == pytest.approx(0.45)
    assert out["net_margin"].value == pytest.approx(0.25)
    assert out["fcf"].value == pytest.approx(121e9 * 0.24)
    assert out["total_debt"].value == pytest.approx(6e9 + 9e9 + 88e9)  # latest balance sheet, all three lines, ONE date
    assert lines["_balance_sheet"]["period_end"] == "2025-12-27"
    assert out["debt_to_equity"].value == pytest.approx(103e9 / 61e9)
    assert out["net_debt"].value == pytest.approx(103e9 - 28e9)
    assert (
        lines["_fiscal_year_end"]["fiscal_year"] == 2025
    )  # label comes from the filing that first reported the period


def test_periods_are_identified_by_their_dates_not_by_fy_labels():
    _, _, index = metrics_of()
    # the FY2025 10-K carries FY2024 and FY2023 comparatives labelled fy=2025; the period's own end date decides
    older = index.select("revenue", end=D(2024, 9, 28), kinds=FY_KINDS)
    assert older.fact is not None and older.fact.fiscal_year == 2025 and older.fact.accession.endswith("25-000100")
    assert index.fiscal_year_label("revenue", D(2024, 9, 28)) == 2024


def test_ttm_is_fy_plus_ytd_minus_prior_ytd_from_one_tag():
    out, _, index = metrics_of()
    ttm = index.ttm("revenue").ttm
    assert ttm.basis == "FY+YTD-YTD_PRIOR" and ttm.period_end == D(2025, 12, 27)
    fy, cur, prior = ttm.components
    assert (fy.period_start, fy.period_end) == (D(2024, 9, 29), D(2025, 9, 27))
    assert (cur.period_start, cur.period_end) == (D(2025, 9, 28), D(2025, 12, 27))
    assert (prior.period_start, prior.period_end) == (D(2024, 9, 29), D(2024, 12, 28))
    assert ttm.value == pytest.approx(fy.value + cur.value - prior.value)
    assert out["revenue_growth_ttm_yoy"].ok and 0.05 < out["revenue_growth_ttm_yoy"].value < 0.15
    assert "TTM_COMPONENTS_FROM_SEVERAL_FILINGS" in out["revenue_growth_ttm_yoy"].flags


def test_ttm_without_a_newer_interim_report_is_the_fiscal_year():
    out, _, index = metrics_of(q1=False)
    ttm = index.ttm("revenue").ttm
    assert ttm.basis == "FY" and ttm.period_end == D(2025, 9, 27)
    assert out["revenue_growth_ttm_yoy"].value == pytest.approx(out["revenue_growth_yoy"].value)


def test_misaligned_interim_periods_are_never_mixed_into_a_ttm():
    doc = syn.standard_issuer(q1=False)
    b = syn.FactsBuilder(320193)
    b.facts = doc["facts"]
    # an interim period that does not start the day after the fiscal year ended (a 3-month period shifted by 3 weeks)
    b.add("RevenueFromContractWithCustomerExcludingAssessedTax", "USD", D(2025, 10, 20), D(2026, 1, 18), 1.0,
          "0000000001-26-000011", D(2026, 1, 30), "10-Q", 2026, "Q1")  # fmt: skip
    index = StatementIndex(facts_of(b.build()))
    outcome = index.ttm("revenue")
    assert (
        outcome.ttm is None
        and outcome.state is MetricState.NOT_COMPARABLE
        and outcome.reason == "TTM_PERIODS_MISALIGNED"
    )


def test_missing_prior_year_ytd_is_missing_input_not_a_shortened_ttm():
    doc = syn.standard_issuer()
    for points in doc["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"]["units"].values():
        points[:] = [p for p in points if p["end"] != "2024-12-28"]
    outcome = StatementIndex(facts_of(doc)).ttm("revenue")
    assert outcome.ttm is None and outcome.reason == "PRIOR_YEAR_YTD_MISSING"


def test_52_53_week_year_lengths_are_fiscal_years():
    _, _, index = metrics_of()
    assert {f.kind for f in index.all_facts("revenue") if f.period_start and f.period_end.month == 9} == set(FY_KINDS)


def test_a_conflicting_second_tag_makes_the_line_unreliable():
    doc = syn.standard_issuer(
        extra=[
            ("Revenues", "USD", D(2024, 9, 29), D(2025, 9, 27), 999.0, "0000000001-25-000100", D(2025, 10, 31), "10-K")
        ]
    )
    out, _, _ = metrics_of(extra=[("Revenues", "USD", D(2024, 9, 29), D(2025, 9, 27), 999.0,
                                    "0000000001-25-000100", D(2025, 10, 31), "10-K")])  # fmt: skip
    assert doc  # the document itself is valid
    assert out["net_margin"].state is MetricState.MISSING_INPUT and out["net_margin"].reason.startswith("TAG_CONFLICT")


def test_a_confirming_second_tag_is_accepted_and_noted():
    index = StatementIndex(
        facts_of(
            syn.standard_issuer(
                extra=[
                    (
                        "Revenues",
                        "USD",
                        D(2024, 9, 29),
                        D(2025, 9, 27),
                        121e9 * 1.001,
                        "0000000001-25-000100",
                        D(2025, 10, 31),
                        "10-K",
                    )
                ]
            )
        )
    )
    selected = index.select("revenue", end=D(2025, 9, 27), kinds=FY_KINDS)
    assert (
        selected.tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
        and "CONFIRMED_BY:Revenues" in selected.flags
    )


def test_company_that_reports_revenue_under_another_tag_is_found():
    out, _, _ = metrics_of(revenue_tag="Revenues")
    assert out["revenue_growth_yoy"].ok and out["net_margin"].ok


def test_missing_capex_is_never_zero_so_fcf_is_missing_input():
    out, _, _ = metrics_of(capex=False)
    assert out["fcf"].state is MetricState.MISSING_INPUT and out["fcf"].reason == "CAPEX_NOT_REPORTED"
    assert out["fcf_growth_yoy"].state is MetricState.MISSING_INPUT


def test_missing_gross_profit_is_missing_not_derived():
    out, _, _ = metrics_of(gross_profit=False)
    assert out["gross_margin"].state is MetricState.MISSING_INPUT and out["operating_margin"].ok


@pytest.mark.parametrize("lines", [(), ("long_term_debt",), ("short_term_debt", "long_term_debt")])
def test_debt_with_any_core_line_absent_is_missing_input(lines):
    out, _, _ = metrics_of(debt_lines=lines)
    assert out["total_debt"].state is MetricState.MISSING_INPUT
    assert out["total_debt"].reason.startswith("DEBT_COMPONENT_ABSENT")
    assert (
        out["net_debt"].state is MetricState.MISSING_INPUT and out["debt_to_equity"].state is MetricState.MISSING_INPUT
    )


def test_an_explicitly_reported_zero_debt_line_counts_but_absence_does_not():
    doc = syn.standard_issuer(debt_lines=())
    b = syn.FactsBuilder(320193)
    b.facts = doc["facts"]
    for tag in ("ShortTermBorrowings", "LongTermDebtCurrent", "LongTermDebtNoncurrent"):
        b.add(tag, "USD", None, D(2025, 12, 27), 0.0, "0000000001-26-000010", D(2026, 1, 30), "10-Q", 2026, "Q1")
    out, _ = fundamental_metrics(StatementIndex(facts_of(b.build())), AS_OF)
    assert out["total_debt"].state is MetricState.OK and out["total_debt"].value == 0.0
    assert out["net_debt"].value == pytest.approx(-28e9)  # net cash


def test_loss_maker_has_no_pe_and_no_growth_from_a_negative_base():
    out, _, index = metrics_of(net_margin=-0.1, eps_base=-3.0)
    assert out["eps_growth_yoy"].state is MetricState.NOT_MEANINGFUL
    assert out["net_margin"].value == pytest.approx(-0.1)  # a negative margin is a valid value
    eps = out["diluted_eps_ttm"]
    assert eps.ok and eps.value < 0
    assert m.price_to_earnings(100.0, eps.value).state is MetricState.NOT_MEANINGFUL


def test_per_share_ttm_across_a_stock_split_is_refused():
    out, _, _ = metrics_of(split_in_q1=10)
    assert out["diluted_eps_ttm"].state is MetricState.NOT_COMPARABLE
    assert out["diluted_eps_ttm"].reason == "POSSIBLE_SPLIT_BASIS_CHANGE"
    assert out["revenue_ttm"].ok  # flow lines are unaffected by a split


def test_per_share_ttm_without_share_counts_cannot_be_checked():
    doc = syn.standard_issuer()
    doc["facts"]["us-gaap"].pop("WeightedAverageNumberOfDilutedSharesOutstanding")
    out, _ = fundamental_metrics(StatementIndex(facts_of(doc)), AS_OF)
    assert (
        out["diluted_eps_ttm"].state is MetricState.MISSING_INPUT
        and out["diluted_eps_ttm"].reason == "SPLIT_GUARD_UNAVAILABLE"
    )


def test_eps_growth_uses_one_filing_even_when_a_later_filing_rebased_the_prior_year():
    """NVDA trap: a later filing shows the prior year on a new per-share basis. Growth must not mix the filings."""
    doc = syn.standard_issuer(q1=False)
    b = syn.FactsBuilder(320193)
    b.facts = doc["facts"]
    # a later filing re-presents FY2024 EPS x10 (split) but NOT FY2025: naive 'latest vintage of each' gives -45%
    b.add("EarningsPerShareDiluted", "USD/shares", D(2023, 10, 1), D(2024, 9, 28), 55.0, "0000000001-26-000050",
          D(2026, 1, 15), "10-K/A", 2025, "FY")  # fmt: skip
    out, _ = fundamental_metrics(StatementIndex(facts_of(b.build())), AS_OF)
    assert out["eps_growth_yoy"].value == pytest.approx(0.10)  # both from the FY2025 10-K


def test_amendment_is_a_separate_filing_and_point_in_time_sees_the_original():
    doc = syn.standard_issuer(q1=False)
    b = syn.FactsBuilder(320193)
    b.facts = doc["facts"]
    b.add(
        "NetIncomeLoss",
        "USD",
        D(2024, 9, 29),
        D(2025, 9, 27),
        1.0e9,
        "0000000001-26-000060",
        D(2026, 1, 20),
        "10-K/A",
        2025,
        "FY",
    )
    index = StatementIndex(facts_of(b.build()))
    current = index.select("net_income_to_common", end=D(2025, 9, 27), kinds=FY_KINDS)
    assert current.value == 1.0e9 and current.fact.form == "10-K/A" and "RESTATED_PERIOD" in current.flags
    before = index.select("net_income_to_common", end=D(2025, 9, 27), kinds=FY_KINDS, as_of=D(2025, 12, 31))
    assert before.fact.form == "10-K" and before.value == pytest.approx(121e9 * 0.25)
    assert before.fact.accession != current.fact.accession  # the original is still there


def test_restated_period_blocks_ttm_growth_instead_of_mixing_bases():
    doc = syn.standard_issuer()
    b = syn.FactsBuilder(320193)
    b.facts = doc["facts"]
    b.add("RevenueFromContractWithCustomerExcludingAssessedTax", "USD", D(2024, 9, 29), D(2025, 9, 27), 130e9,
          "0000000001-26-000061", D(2026, 1, 25), "10-K/A", 2025, "FY")  # fmt: skip
    out, _ = fundamental_metrics(StatementIndex(facts_of(b.build())), AS_OF)
    assert (
        out["revenue_growth_ttm_yoy"].state is MetricState.NOT_COMPARABLE
        and out["revenue_growth_ttm_yoy"].reason == "RESTATED_COMPONENT"
    )


def test_shares_are_used_only_when_reliable():
    index = StatementIndex(facts_of(syn.standard_issuer()))
    assert index.shares_outstanding(AS_OF) == (14.6e9, None)
    assert index.shares_outstanding(D(2027, 6, 1))[1].startswith("STALE_SHARES")  # BRK-B style stale dei fact
    ambiguous = syn.standard_issuer(dei_shares=0)
    b = syn.FactsBuilder(320193)
    b.facts = ambiguous["facts"]
    for value in (5.8e9, 5.5e9):  # classes collapsed into one undimensioned series
        b.add("EntityCommonStockSharesOutstanding", "shares", None, D(2025, 10, 17), value, "0000000001-25-000100",
              D(2025, 10, 31), "10-K", 2025, "FY", taxonomy="dei")  # fmt: skip
    assert StatementIndex(facts_of(b.build())).shares_outstanding(AS_OF) == (None, "MULTI_CLASS_AMBIGUOUS")
    assert StatementIndex([]).shares_outstanding(AS_OF) == (None, "NOT_REPORTED")


def test_net_income_is_the_parent_line_never_profit_loss():
    doc = syn.standard_issuer()
    point = {
        "end": "2025-09-27",
        "start": "2024-09-29",
        "val": 1.0,
        "accn": "x-25-1",
        "filed": "2025-10-31",
        "form": "10-K",
    }
    doc["facts"]["us-gaap"]["ProfitLoss"] = {"units": {"USD": [point]}}
    parsed = parse_companyfacts(doc, CIK)
    assert "ProfitLoss" not in {p.concept for p in parsed.points}  # not even stored: it is a different line


def test_no_facts_at_all_gives_missing_input_everywhere_not_zeros():
    out, lines = fundamental_metrics(StatementIndex([]), AS_OF)
    assert all(not r.ok and r.state is MetricState.MISSING_INPUT for r in out.values())
    assert out["total_debt"].reason == "NO_BALANCE_SHEET"
