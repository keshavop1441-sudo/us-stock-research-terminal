"""Short-term debt concept coverage (CommercialPaper) and full provenance of composed TTM metrics.

Synthetic SEC-shaped facts (tests/p0_synthetic.py) only; nothing here claims what any real filer tags. Rules under
test: ``CommercialPaper`` counts as short-term debt only where ``ShortTermBorrowings`` is not reported for the SAME
balance-sheet date; disagreeing tags make the line unavailable; absence is never zero; ``DebtCurrent`` is not mapped;
every TTM metric names every filing/period/tag it was composed from, and that survives into the evidence packet.
"""

from datetime import date

import p0_synthetic as syn
import pytest
from test_fundamentals import AS_OF, facts_of

from app.models.concepts import SPEC_BY_LINE, SPEC_BY_TAG
from app.screening.fundamentals import StatementIndex
from app.screening.metrics import MetricState
from app.screening.snapshot import fundamental_metrics

D = date
FY25, FY24, Q1 = D(2025, 9, 27), D(2024, 9, 28), D(2025, 12, 27)
K25, K24, KQ = "0000000001-25-000100", "0000000001-24-000100", "0000000001-26-000010"
NO_SHORT_TERM = ("current_portion_long_term_debt", "long_term_debt")  # standard_issuer without the short-term line
DEBT_METRICS = ("total_debt", "net_debt", "debt_to_equity", "debt_to_equity_fy", "debt_to_equity_prior_fy",
                "debt_to_equity_change_yoy")  # fmt: skip


def instant(tag, end, value, accn, filed, form="10-K"):
    return (tag, "USD", None, end, value, accn, filed, form)


def commercial_paper(tag="CommercialPaper", values=(6e9, 5e9, 4e9)):
    """Short-term debt facts at the latest 10-Q date and both fiscal year ends (as an AAPL-style filer reports)."""
    q1, fy25, fy24 = values
    return (
        instant(tag, Q1, q1, KQ, D(2026, 1, 30), "10-Q"),
        instant(tag, FY25, fy25, K25, D(2025, 10, 31)),
        instant(tag, FY24, fy24, K25, D(2025, 10, 31)),
    )


def run(extra=(), debt_lines=NO_SHORT_TERM):
    index = StatementIndex(facts_of(syn.standard_issuer(debt_lines=debt_lines, extra=extra)))
    out, lines = fundamental_metrics(index, AS_OF)
    return out, lines, index


# --- debt concept coverage -------------------------------------------------------------------------------------------


def test_commercial_paper_is_mapped_to_short_term_debt_and_debt_current_is_not():
    assert SPEC_BY_TAG[("us-gaap", "CommercialPaper")].line == "short_term_debt"
    assert SPEC_BY_LINE["short_term_debt"].tags == ("ShortTermBorrowings", "CommercialPaper")
    assert ("us-gaap", "DebtCurrent") not in SPEC_BY_TAG  # it contains the current portion of LTD: would double count


def test_filer_with_only_commercial_paper_gets_every_debt_metric():
    out, lines, _ = run(commercial_paper())
    # latest balance sheet 2025-12-27: CP 6 + current LTD 9 + LTD 88 (billions), equity 61, cash 28, no STI line
    assert out["total_debt"].value == pytest.approx(103e9)
    assert out["net_debt"].value == pytest.approx(103e9 - 28e9)
    assert out["debt_to_equity"].value == pytest.approx(103e9 / 61e9)
    # fiscal year ends come from the FY2025 10-K only: CP 5 + 10 + 90, equity 60e9+2025; prior 4 + 10 + 90, 60e9+2024
    now, prior = 105e9 / (60e9 + 2025), 104e9 / (60e9 + 2024)
    assert out["debt_to_equity_fy"].value == pytest.approx(now)
    assert out["debt_to_equity_prior_fy"].value == pytest.approx(prior)
    assert out["debt_to_equity_change_yoy"].value == pytest.approx(now - prior)
    assert all(out[name].state is MetricState.OK for name in DEBT_METRICS)
    rec = lines["short_term_debt"]  # provenance names the tag actually used
    assert (rec["tag"], rec["value"], rec["accession"], rec["form"], rec["period_end"]) == (
        "CommercialPaper", 6e9, KQ, "10-Q", "2025-12-27")  # fmt: skip


def test_short_term_borrowings_is_still_used_and_commercial_paper_only_confirms_it():
    both = (*commercial_paper(values=(6e9, 5e9, 5e9)),)
    out, lines, _ = run(
        (
            *both,
            instant("ShortTermBorrowings", Q1, 6e9, KQ, D(2026, 1, 30), "10-Q"),
            instant("ShortTermBorrowings", FY25, 5e9, K25, D(2025, 10, 31)),
            instant("ShortTermBorrowings", FY24, 5e9, K25, D(2025, 10, 31)),
        )
    )
    assert out["total_debt"].value == pytest.approx(103e9)
    assert lines["short_term_debt"]["tag"] == "ShortTermBorrowings"
    assert "CONFIRMED_BY:CommercialPaper" in lines["short_term_debt"]["flags"]


def test_explicit_zero_short_term_borrowings_is_a_value_and_counts():
    zero = (
        instant("ShortTermBorrowings", Q1, 0.0, KQ, D(2026, 1, 30), "10-Q"),
        instant("ShortTermBorrowings", FY25, 0.0, K25, D(2025, 10, 31)),
        instant("ShortTermBorrowings", FY24, 0.0, K25, D(2025, 10, 31)),
    )
    out, _, _ = run(zero)
    assert out["total_debt"].value == pytest.approx(97e9)
    assert all(out[name].state is MetricState.OK for name in DEBT_METRICS)


def test_filer_reporting_no_short_term_debt_concept_is_debt_from_the_long_term_lines_only():
    """MSFT/NVDA shape: short-term debt is optional. Latest sheet 2025-12-27: current LTD 9 + LTD 88, no tag added."""
    out, lines, _ = run()
    assert out["total_debt"].ok and out["total_debt"].value == pytest.approx(97e9)  # current portion counted ONCE
    assert "SHORT_TERM_DEBT_NOT_REPORTED" in out["total_debt"].flags
    assert all(out[name].state is MetricState.OK for name in DEBT_METRICS)
    assert lines["short_term_debt"] == {"value": None, "reason": "NOT_REPORTED"}  # provenance stays NOT_REPORTED
    assert lines["current_portion_long_term_debt"]["tag"] == "LongTermDebtCurrent"
    assert lines["current_portion_long_term_debt"]["value"] == 9e9
    assert lines["long_term_debt"]["tag"] == "LongTermDebtNoncurrent"


def test_missing_long_term_components_still_block_total_debt():
    for dropped, reason in (
        ("current_portion_long_term_debt", "DEBT_COMPONENT_ABSENT:current_portion_long_term_debt"),
        ("long_term_debt", "DEBT_COMPONENT_ABSENT:long_term_debt"),
    ):
        keep = tuple(x for x in NO_SHORT_TERM if x != dropped)
        out, _, _ = run(commercial_paper(), debt_lines=keep)  # a reported CP does not stand in for a core line
        assert out["total_debt"].state is MetricState.MISSING_INPUT and out["total_debt"].reason == reason
        for name in ("net_debt", "debt_to_equity"):
            assert out[name].state is MetricState.MISSING_INPUT and out[name].value is None, name


def test_no_new_short_term_debt_tag_is_mapped():
    assert SPEC_BY_LINE["short_term_debt"].tags == ("ShortTermBorrowings", "CommercialPaper")
    assert SPEC_BY_LINE["current_portion_long_term_debt"].tags == ("LongTermDebtCurrent",)
    assert SPEC_BY_LINE["long_term_debt"].tags == ("LongTermDebtNoncurrent",)


def test_debt_current_alone_is_not_taken_as_short_term_debt():
    extra = (instant("DebtCurrent", Q1, 15e9, KQ, D(2026, 1, 30), "10-Q"),)
    out, lines, _ = run(extra)
    assert out["total_debt"].value == pytest.approx(97e9)  # DebtCurrent (15e9) is neither added nor substituted
    assert lines["short_term_debt"]["reason"] == "NOT_REPORTED"


def test_commercial_paper_from_another_date_is_not_carried_onto_the_balance_sheet_date():
    """Period alignment: CP only at the fiscal year ends does not fill the (later) latest balance sheet date."""
    extra = commercial_paper()[1:]
    out, _, _ = run(extra)
    assert out["total_debt"].value == pytest.approx(97e9)  # latest sheet is 2025-12-27: no CP there, none carried over
    assert "SHORT_TERM_DEBT_NOT_REPORTED" in out["total_debt"].flags
    assert out["debt_to_equity_fy"].state is MetricState.OK  # the fiscal-year pair is complete in the 10-K


def test_disagreeing_short_term_tags_make_the_line_unavailable_instead_of_picking_one():
    out, _, _ = run(
        (
            *commercial_paper(values=(4e9, 4e9, 4e9)),
            instant("ShortTermBorrowings", Q1, 6e9, KQ, D(2026, 1, 30), "10-Q"),
            instant("ShortTermBorrowings", FY25, 6e9, K25, D(2025, 10, 31)),
            instant("ShortTermBorrowings", FY24, 6e9, K25, D(2025, 10, 31)),
        )
    )
    assert out["total_debt"].state is MetricState.MISSING_INPUT
    assert out["total_debt"].reason == "DEBT_LINE_TAG_CONFLICT:ShortTermBorrowings!=CommercialPaper"
    for name in DEBT_METRICS:
        assert not out[name].ok and out[name].value is None, name


# --- TTM provenance --------------------------------------------------------------------------------------------------


def fields(rec):
    return [
        (c["role"], c["sign"], c["tag"], c["accession"], c["period_start"], c["period_end"]) for c in rec["components"]
    ]


def test_composed_ttm_revenue_names_every_filing_period_and_tag_it_used():
    out, lines, _ = run()
    rec = lines["_ttm_revenue"]
    assert fields(rec) == [
        ("fiscal_year", 1, "RevenueFromContractWithCustomerExcludingAssessedTax", K25, "2024-09-29", "2025-09-27"),
        ("current_ytd", 1, "RevenueFromContractWithCustomerExcludingAssessedTax", KQ, "2025-09-28", "2025-12-27"),
        ("prior_year_ytd", -1, "RevenueFromContractWithCustomerExcludingAssessedTax", KQ, "2024-09-29", "2024-12-28"),
    ]  # fmt: skip
    assert rec["basis"] == "FY+YTD-YTD_PRIOR" and rec["accessions"] == sorted({K25, KQ})
    fy, cur, prior = (c["value"] for c in rec["components"])
    assert rec["value"] == pytest.approx(fy + cur - prior) == pytest.approx(out["revenue_ttm"].value)
    assert {"form": "10-K"}.items() <= rec["components"][0].items()
    assert {"form": "10-Q"}.items() <= rec["components"][1].items()
    assert all(c["filed"] for c in rec["components"])


def test_ttm_calculation_is_unchanged_by_the_provenance():
    out, _, index = run()
    outcome = index.ttm("revenue")
    assert out["revenue_ttm"].value == outcome.ttm.value
    fy, cur, prior = outcome.ttm.components
    assert out["revenue_ttm"].value == fy.value + cur.value - prior.value


def test_ttm_eps_provenance_includes_components_and_split_guard_facts():
    out, lines, _ = run()
    rec = lines["_ttm_diluted_eps"]
    assert [c["role"] for c in rec["components"]] == ["fiscal_year", "current_ytd", "prior_year_ytd"]
    assert {c["tag"] for c in rec["components"]} == {"EarningsPerShareDiluted"}
    assert rec["value"] == pytest.approx(out["diluted_eps_ttm"].value)
    guards = rec["guard_components"]
    assert [g["tag"] for g in guards] == ["WeightedAverageNumberOfDilutedSharesOutstanding"] * 2
    assert {g["accession"] for g in guards} == {K25, KQ}
    assert "PER_SHARE_SUM_OF_PERIODS" in rec["flags"]


def test_ttm_growth_exposes_the_prior_year_ttm_components_too():
    out, lines, _ = run()
    prior = lines["_ttm_revenue_prior"]
    assert [c["role"] for c in prior["components"]] == ["fiscal_year", "current_ytd", "prior_year_ytd"]
    assert prior["components"][0]["period_end"] == "2024-09-28"
    assert "0000000001-25-000010" in prior["accessions"]  # the 10-Q one year earlier
    assert out["revenue_growth_ttm_yoy"].ok


def test_ttm_that_is_just_the_fiscal_year_has_one_component():
    out, lines, _ = metrics(q1=False)
    rec = lines["_ttm_revenue"]
    assert rec["basis"] == "FY" and fields(rec)[0][:3] == ("fiscal_year", 1, rec["tag"]) and len(rec["components"]) == 1
    assert rec["accessions"] == [K25] and rec["guard_components"] == []


def metrics(**kwargs):
    index = StatementIndex(facts_of(syn.standard_issuer(**kwargs)))
    out, lines = fundamental_metrics(index, AS_OF)
    return out, lines, index


def test_unavailable_ttm_records_the_reason_not_components():
    out, lines, _ = metrics(split_in_q1=10)
    rec = lines["_ttm_diluted_eps"]
    assert rec["value"] is None and rec["reason"] == "POSSIBLE_SPLIT_BASIS_CHANGE" and "components" not in rec
    assert out["diluted_eps_ttm"].state is MetricState.NOT_COMPARABLE


def test_every_catalogued_ttm_metric_points_at_its_composition_lines():
    from app.research.catalog import CATALOG

    assert CATALOG["revenue_ttm"].lines == ("_ttm_revenue",)
    assert CATALOG["revenue_growth_ttm_yoy"].lines == ("_ttm_revenue", "_ttm_revenue_prior")
    assert CATALOG["diluted_eps_ttm"].lines == ("_ttm_diluted_eps",)
    assert CATALOG["price_to_sales_ttm"].lines == ("_ttm_revenue",)
    assert CATALOG["price_to_earnings"].lines == ("_ttm_diluted_eps",)


# --- short-term investments: DebtSecuritiesCurrent (NVIDIA-style current marketable debt securities) -----------------


def sti_facts(tag, q1=34.143e9, fy25=30e9, fy24=25e9):
    return (
        instant(tag, Q1, q1, KQ, D(2026, 1, 30), "10-Q"),
        instant(tag, FY25, fy25, K25, D(2025, 10, 31)),
        instant(tag, FY24, fy24, K25, D(2025, 10, 31)),
    )


def test_debt_securities_current_is_a_short_term_investments_candidate_and_no_equity_concept_is():
    assert SPEC_BY_LINE["short_term_investments"].tags == (
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
        "DebtSecuritiesCurrent",
    )
    assert SPEC_BY_TAG[("us-gaap", "DebtSecuritiesCurrent")].line == "short_term_investments"
    for equity_tag in ("EquitySecuritiesFvNi", "MarketableSecuritiesEquitySecurities", "EquitySecuritiesFvNiCurrent"):
        assert ("us-gaap", equity_tag) not in SPEC_BY_TAG, equity_tag


def test_debt_securities_current_is_used_when_the_other_candidates_are_absent():
    out, lines, _ = run(sti_facts("DebtSecuritiesCurrent"))
    rec = lines["short_term_investments"]
    assert (rec["tag"], rec["value"], rec["accession"], rec["form"], rec["period_end"]) == (
        "DebtSecuritiesCurrent", 34.143e9, KQ, "10-Q", "2025-12-27")  # fmt: skip
    # net debt = debt 97 - (cash 28 + investments 34.143): the investments are subtracted, no ASSUMED_ZERO flag
    assert out["net_debt"].value == pytest.approx(97e9 - (28e9 + 34.143e9))
    assert "ASSUMED_ZERO:short_term_investments" not in out["net_debt"].flags
    assert out["total_debt"].value == pytest.approx(97e9)  # debt aggregation untouched


def test_existing_investment_tags_still_win_and_conflicts_are_still_detected():
    out, lines, _ = run(sti_facts("ShortTermInvestments", q1=20e9, fy25=20e9, fy24=20e9))
    assert lines["short_term_investments"]["tag"] == "ShortTermInvestments"
    assert out["net_debt"].value == pytest.approx(97e9 - 48e9)
    agree, lines, _ = run(
        (*sti_facts("ShortTermInvestments", 20e9, 20e9, 20e9), *sti_facts("DebtSecuritiesCurrent", 20e9, 20e9, 20e9))
    )
    assert lines["short_term_investments"]["tag"] == "ShortTermInvestments"
    assert "CONFIRMED_BY:DebtSecuritiesCurrent" in lines["short_term_investments"]["flags"]
    clash, _, _ = run(
        (
            *sti_facts("MarketableSecuritiesCurrent", 20e9, 20e9, 20e9),
            *sti_facts("DebtSecuritiesCurrent", 34e9, 34e9, 34e9),
        )
    )
    assert clash["net_debt"].state is MetricState.MISSING_INPUT and clash["net_debt"].reason.startswith(
        "CASH_TAG_CONFLICT"
    )


def test_an_equity_security_fact_is_not_taken_as_a_short_term_investment():
    out, lines, _ = run(sti_facts("EquitySecuritiesFvNi", 42.783e9))
    assert lines["short_term_investments"] == {"value": None, "reason": "NOT_REPORTED"}
    assert "ASSUMED_ZERO:short_term_investments" in out["net_debt"].flags
    assert out["net_debt"].value == pytest.approx(97e9 - 28e9)
