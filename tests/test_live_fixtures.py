"""Facts and rules pinned against values the audit probe retrieved LIVE on 2026-10-02 (GitHub-hosted runner).

tests/fixtures/live_probe_2026_10_02 holds values transcribed from the probe's log lines (see PROVENANCE.json for the
run and job ids). They prove payload shape and provider behaviour at that moment, not today's market data. Each test
ties a recorded provider behaviour to the metric rule that exists because of it.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from app.models.periods import parse_fiscal_year_end, parse_iso_date, parse_month_year
from app.models.symbols import UnknownProviderError, canonical_symbol, provider_symbol
from app.screening import metrics as m
from app.screening.metrics import FactPoint, Form4Class, MetricState

SAMPLES = Path(__file__).resolve().parent / "fixtures" / "live_probe_2026_10_02"


def load(name: str):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def test_every_live_fixture_has_provenance_with_run_id():
    provenance = load("PROVENANCE.json")
    files = {p.name for p in SAMPLES.iterdir() if p.name != "PROVENANCE.json"}
    assert files == set(provenance)
    for name, entry in provenance.items():
        runs = entry["evidence_run"] if isinstance(entry["evidence_run"], list) else [entry["evidence_run"]]
        assert runs and all(isinstance(r["run"], int) and len(r["sha"]) == 40 for r in runs), name
        assert "names omitted" in entry["licence_note"] or "no personal data" in entry["licence_note"], name


# --- per-share vintage trap (NVDA) ---------------------------------------------------------------------------------
def test_naive_eps_growth_across_default_rows_is_wrong_and_same_filing_pair_is_the_rule():
    data = load("nvda_eps_vintage.json")
    default = {r["period_ending"]: r for r in data["pit_false"]}
    # default mode: FY2024 EPS is post-split (1.19) but FY2023 is still pre-split (1.74): "growth" of -31.6% for a
    # company whose net income grew 6.8x. This is the number a naive screener would have produced.
    naive = m.growth_rate(default["2024-01-28"]["diluted_eps"], default["2023-01-29"]["diluted_eps"])
    assert naive.state is MetricState.OK and naive.value == pytest.approx(1.19 / 1.74 - 1)
    income_growth = m.growth_rate(default["2024-01-28"]["net_income"], default["2023-01-29"]["net_income"])
    assert income_growth.value == pytest.approx(29760 / 4368 - 1), (
        "net income grew ~6.8x, so a negative EPS 'growth' is a basis mismatch, not a decline"
    )
    # pit mode shows the as-filed FY2024 value (11.93): two vintages of ONE period differ by exactly the split ratio
    assert data["pit_true"][2]["diluted_eps"] / default["2024-01-28"]["diluted_eps"] == pytest.approx(10.0, rel=0.01)


def test_same_filing_pair_never_mixes_vintages():
    # Modelled on the NVDA case: filing A (FY2024 10-K, pre-split) and filing B (a later filing, post-split basis)
    fy24, fy23 = date(2024, 1, 28), date(2023, 1, 29)
    points = [
        FactPoint(11.93, date(2023, 1, 30), fy24, date(2024, 2, 21), "A", "10-K"),
        FactPoint(1.74, date(2022, 1, 31), fy23, date(2024, 2, 21), "A", "10-K"),
        FactPoint(
            1.19, date(2023, 1, 30), fy24, date(2025, 2, 26), "B", "10-K"
        ),  # restated for the split, no FY23 here
    ]
    pair = m.same_filing_pair(points, fy24, fy23)
    assert pair is not None and (pair[0].value, pair[1].value) == (11.93, 1.74) and pair[0].accession == "A"
    assert m.same_filing_pair(points[2:], fy24, fy23) is None  # a lone vintage cannot supply a prior-year value
    # point-in-time: before filing A existed there is no pair at all
    assert m.same_filing_pair(points, fy24, fy23, as_of=date(2024, 1, 31)) is None


# --- net income attribution (RIVN) ---------------------------------------------------------------------------------
def test_rivn_net_income_has_two_definitions_and_the_dictionary_picks_attributable_to_parent():
    data = load("rivn_net_income.json")
    latest = data["openbb"][0]
    assert latest["net_income"] == -3626000000.0 and latest["net_income_to_common"] == -3646000000.0
    # the raw tags confirm which is which: ProfitLoss = consolidated, NetIncomeLoss = attributable to the parent
    assert data["raw_companyfacts_fy"]["ProfitLoss"][-1][1] == latest["net_income"]
    assert data["raw_companyfacts_fy"]["NetIncomeLoss"][-1][1] == latest["net_income_to_common"]
    revenue = data["income_annual_2025"]["total_revenue"]
    consolidated = m.margin(latest["net_income"], revenue).value
    attributable = m.margin(latest["net_income_to_common"], revenue).value
    assert attributable < consolidated < 0  # both negative margins are legitimate values, not N/A
    assert attributable == pytest.approx(-3646 / 5387, rel=1e-6)


def test_rivn_loss_making_cases_return_states_not_numbers():
    data = load("rivn_net_income.json")
    y25, y24 = data["income_annual_2025"], data["income_annual_2024"]
    assert (
        m.growth_rate(y25["total_gross_profit"], y24["total_gross_profit"]).reason == "SIGN_CHANGE"
    )  # -1.2B -> +0.14B
    assert m.growth_rate(y25["total_operating_income"], y24["total_operating_income"]).reason == "NEGATIVE_BASE"
    assert m.growth_rate(y25["diluted_eps"], y24["diluted_eps"]).state is MetricState.NOT_MEANINGFUL  # -3.07 vs -4.69
    assert m.price_to_earnings(20.0, y25["diluted_eps"]).state is MetricState.NOT_MEANINGFUL
    gross_margin_24 = m.margin(y24["total_gross_profit"], y24["total_revenue"])
    assert gross_margin_24.state is MetricState.OK and gross_margin_24.value == pytest.approx(-1200 / 4970)
    assert (
        m.margin_change(m.margin(y25["total_gross_profit"], y25["total_revenue"]), gross_margin_24).state
        is MetricState.OK
    )


# --- Berkshire: absent lines are not zeros ------------------------------------------------------------------------
def test_berkshire_without_debt_lines_is_missing_not_debt_free():
    data = load("brkb_statements.json")
    row = data["balance_annual"][0]
    assert row["total_assets"] > 1e12 and row["long_term_debt"] is None
    debt = m.total_debt(
        row["short_term_debt"],
        row["current_portion_of_long_term_debt"],
        row["long_term_debt"],
        balance_sheet_present=True,
    )
    assert debt.state is MetricState.MISSING_INPUT and debt.reason.startswith("DEBT_COMPONENT_ABSENT:")
    assert m.debt_to_equity(debt, row["total_equity"]).state is MetricState.MISSING_INPUT
    assert (
        m.net_debt(debt, row["cash_and_equivalents"], row["short_term_investments"]).state is MetricState.MISSING_INPUT
    )
    assert "diluted_eps" in data["income_row_absent_fields"]  # no EPS either: P/E is MISSING, not computed from guesses


def test_apple_debt_components_all_reported_give_a_total():
    # AAPL FY2025 balance sheet, as returned live (fundamentals run 36954761165, sec.balance.AAPL.annual)
    debt = m.total_debt(7979e6, 12350e6, 78328e6, balance_sheet_present=True)
    assert debt.state is MetricState.OK and debt.value == pytest.approx(98657e6)
    assert debt.flags == ("FINANCE_LEASES_NOT_REPORTED",)  # lease line not passed: left out and flagged, not zeroed
    assert m.net_debt(debt, 35934e6, 18763e6).value == pytest.approx(98657e6 - 35934e6 - 18763e6)


def test_company_types_seen_live():
    types = load("company_types.json")["company_type"]
    assert types["BRK-B"] == "diversified" and types["JPM"] == "financial"
    assert {types[k] for k in ("AAPL", "GOOGL", "NVDA", "RIVN")} == {"industrial"}


# --- multi-class issuers (GOOGL/GOOG, BRK.A/BRK.B) -----------------------------------------------------------------
def test_market_cap_is_per_quote_price_times_all_shares_so_listings_are_never_summed():
    quotes = {q["symbol"]: q for q in load("multiclass_quotes.json")["quotes"]}
    implied = {s: q["market_cap"] / q["last_price"] for s, q in quotes.items()}
    assert implied["GOOGL"] == pytest.approx(implied["GOOG"], rel=0.001)  # same share count behind both quotes
    caps = {s: q["market_cap"] for s, q in quotes.items() if s in ("GOOGL", "GOOG")}
    naive_sum = sum(caps.values())
    chosen = m.issuer_market_cap(caps, "GOOGL")
    assert chosen.value == caps["GOOGL"] and "MULTI_CLASS_ALL_SHARES_AT_PRIMARY_PRICE" in chosen.flags
    assert naive_sum > 1.9 * chosen.value  # the double count the rule prevents
    assert m.issuer_market_cap({"AAPL": quotes["AAPL"]["market_cap"]}, "AAPL").flags == ()
    assert m.issuer_market_cap({"GOOG": 1.0}, "GOOGL").state is MetricState.MISSING_INPUT


def test_every_listing_of_a_multi_class_issuer_is_screened_on_the_same_issuer_market_cap():
    quotes = {q["symbol"]: q["market_cap"] for q in load("multiclass_quotes.json")["quotes"]}
    googl_family = {k: quotes[k] for k in ("GOOGL", "GOOG")}
    brk_family = {k: quotes[k] for k in ("BRK.A", "BRK.B")}
    # P/S for the GOOG row and the GOOGL row use one issuer cap (the designated primary listing's), hence one ratio
    ratios = {
        listing: m.price_to_sales(m.issuer_market_cap(googl_family, "GOOGL").value, 400e9).value
        for listing in googl_family
    }
    assert ratios["GOOG"] == ratios["GOOGL"] == pytest.approx(googl_family["GOOGL"] / 400e9)
    assert (
        m.issuer_market_cap(brk_family, "BRK.B").value == brk_family["BRK.B"]
    )  # primary choice is explicit, not implied
    # the per-quote figures differ, which is exactly why one primary must be designated
    assert googl_family["GOOGL"] != googl_family["GOOG"]


def test_share_count_sources_differ_by_class_structure():
    shares = load("dei_shares.json")
    aapl = shares["AAPL"]["latest"][-1]
    assert aapl[1] == 14594180000 and aapl[2] == "10-Q"  # single class: dei carries a shares-outstanding fact
    assert "EntityCommonStockSharesOutstanding" not in shares["GOOGL"]["dei_tags"]  # multi-class: only the float
    assert shares["BRK_B"]["last_end"] < "2012"  # stale: the dei fact stopped being tagged without a class axis


def test_sec_ttm_weighted_shares_are_a_four_quarter_sum_and_must_not_be_used():
    data = load("aapl_ttm_and_quarterly.json")
    ttm_shares = data["income_ttm"]["weighted_ave_diluted_shares_os"]
    outstanding = load("dei_shares.json")["AAPL"]["latest"][-1][1]
    assert 3.9 < ttm_shares / outstanding < 4.2  # ~4x the real share count
    # the TTM EPS itself is coherent (net income / true shares) even though the share field is not
    assert data["income_ttm"]["net_income"] / outstanding == pytest.approx(data["income_ttm"]["diluted_eps"], rel=0.03)


# --- cash flow -----------------------------------------------------------------------------------------------------
def test_quarterly_cash_flow_is_discrete_and_four_quarters_equal_the_ttm_figure():
    data = load("aapl_ttm_and_quarterly.json")
    last_four = sum(q["value"] for q in data["operating_cash_flow_quarterly"][:4])
    assert last_four == data["cashflow_ttm"]["net_cash_from_operating_activities"]
    ttm = data["cashflow_ttm"]
    fcf = m.free_cash_flow(ttm["net_cash_from_operating_activities"], ttm["purchase_of_plant_property_and_equipment"])
    assert fcf.value == pytest.approx(146724e6 - 10041e6)  # capex arrives NEGATIVE; the rule uses its magnitude
    annual = data["annual"]
    assert m.free_cash_flow(
        annual["net_cash_from_operating_activities"], annual["purchase_of_plant_property_and_equipment"]
    ).value == pytest.approx(111482e6 - 12715e6)
    assert m.free_cash_flow(1.0, None).state is MetricState.MISSING_INPUT  # absent capex is never assumed zero


# --- price history conventions -------------------------------------------------------------------------------------
def test_providers_agree_closes_are_split_adjusted_but_not_dividend_adjusted():
    for symbol, by_provider in load("week52_and_adjustment.json")["close_same_dates"].items():
        assert by_provider["cboe"] == by_provider["nasdaq"], symbol  # identical on shared dates, so one convention


def test_52_week_range_matches_the_providers_published_year_range():
    for row in load("week52_and_adjustment.json")["week52"]:
        assert row["calc_high_by_high"] == row["quote_year_high"] and row["calc_low_by_low"] == row["quote_year_low"]
        assert row["calc_high_by_close"] < row["calc_high_by_high"]  # close-based high is a different, lower number


def test_cboe_history_is_deeper_than_nasdaq_and_both_start_after_a_recent_ipo():
    depth = load("week52_and_adjustment.json")["history_depth"]
    assert depth["cboe_AAPL_first"] < depth["nasdaq_AAPL_first"]
    rivn = [(parse_iso_date(depth["nasdaq_RIVN_first"]), 100.0), (date(2026, 9, 30), 90.0)]
    assert m.lookback_return(rivn, date(2026, 9, 30), years=5).reason == "INSUFFICIENT_HISTORY"


# --- symbols -------------------------------------------------------------------------------------------------------
def test_share_class_spellings_per_provider():
    spelling = load("week52_and_adjustment.json")["symbol_spelling"]
    assert spelling["sec_cik_map"]["BRK-B"] == "0001067983"
    for text in ("BRK.B", "BRK/B", "brk-b", " BRK-B "):
        assert canonical_symbol(text) == "BRK-B"
    assert provider_symbol("BRK-B", "sec") == "BRK-B"
    assert provider_symbol("BRK-B", "nasdaq") == provider_symbol("BRK-B", "cboe") == "BRK.B"
    assert spelling["nasdaq_quote"]["BRK.B"] == "ok" and spelling["cboe_quote"]["BRK.B"] == "ok"
    assert spelling["nasdaq_quote"]["BRK/B"] != "ok" and spelling["cboe_quote"]["BRK-B"] != "ok"


def test_plain_and_unverified_symbols_pass_through_unchanged():
    assert canonical_symbol("aapl") == "AAPL"
    assert canonical_symbol("GOOGL") == "GOOGL"
    assert canonical_symbol("BAC-PL") == "BAC-PL"  # preferred-share convention not verified: left alone
    assert provider_symbol("AAPL", "cboe") == "AAPL"
    with pytest.raises(UnknownProviderError):
        provider_symbol("AAPL", "yfinance")


def test_symbol_directory_facts_for_share_classes():
    ident = load("identity.json")
    assert (
        ident["company_tickers_exchange_lookup"]["GOOG"][0]
        == ident["company_tickers_exchange_lookup"]["GOOGL"][0]
        == 1652044
    )
    assert len(ident["submissions"]["GOOGL"]["tickers"]) > 2  # GOOGM/GOOGN also listed: not every ticker is a class
    assert ident["company_tickers_exchange_lookup"]["TWTR"] is None and ident["submissions"]["TWTR"]["tickers"] == []
    assert ident["submissions"]["TWTR"]["cik"] == "0001418091"  # a discontinued ticker still resolves by CIK
    assert ident["submissions"]["META"]["formerNames"][0]["name"] == "Facebook Inc"
    assert ident["company_tickers_exchange_lookup"]["FB"] is None  # the old ticker is gone from the current map


# --- date / period normalisation -----------------------------------------------------------------------------------
def test_fiscal_year_end_parsing_and_non_calendar_filers():
    submissions = load("identity.json")["submissions"]
    assert parse_fiscal_year_end(submissions["AAPL"]["fiscalYearEnd"]) == (9, 26)
    assert parse_fiscal_year_end(submissions["NVDA"]["fiscalYearEnd"]) == (1, 31)
    assert parse_fiscal_year_end(submissions["COST"]["fiscalYearEnd"]) == (8, 30)
    assert parse_fiscal_year_end(submissions["BRK-B"]["fiscalYearEnd"]) == (12, 31)
    assert parse_fiscal_year_end(None) is None and parse_fiscal_year_end("  ") is None
    for bad in ("926", "1332", "0230", "09-26", "abcd"):
        with pytest.raises(ValueError, match="fiscalYearEnd"):
            parse_fiscal_year_end(bad)
    assert parse_fiscal_year_end("0229") == (2, 29)


def test_month_year_and_iso_date_spellings():
    assert parse_month_year("Jun 2026") == date(2026, 6, 30)
    assert parse_month_year("2026-02") == date(2026, 2, 28)
    assert parse_month_year("Feb 2024") == date(2024, 2, 29)
    assert parse_iso_date("2026-10-01T00:00:00") == date(2026, 10, 1)
    for bad in ("2026", "June", "13/2026", "2026-13"):
        with pytest.raises(ValueError):
            parse_month_year(bad)


# --- insiders ------------------------------------------------------------------------------------------------------
def test_openbb_insider_descriptions_classify_and_only_market_trades_are_signals():
    data = load("insider_and_institutional.json")
    counts = {
        Form4Class(m.classify_form4_description(t)): n for t, n in data["sec_insider_description_counts_AAPL"].items()
    }
    assert counts[Form4Class.AWARD] == 13 and counts[Form4Class.OPEN_MARKET_SELL] == 8
    assert counts[Form4Class.OPTION_EXERCISE_OR_CONVERSION] == 2 and counts[Form4Class.TAX_WITHHOLDING] == 1
    signals = sum(n for kind, n in counts.items() if m.is_market_signal(kind))
    assert signals == 8, "of 24 AAPL rows only the open-market sales are discretionary market signals"
    assert m.classify_form4_description(data["nasdaq_insider_row"]["transaction_type"]) is Form4Class.OPEN_MARKET_SELL
    sale, grant = data["sec_insider_rows"]
    assert sale["transaction_value"] is None  # OpenBB never fills it: value = price x shares must be derived
    assert sale["transaction_price"] * sale["securities_transacted"] == pytest.approx(806_000, rel=0.01)
    assert grant["transaction_price"] == 0.0 and grant["security_type"] == "Restricted Stock Unit"


def test_institutional_rows_with_mixed_periods_are_not_comparable():
    rows = load("insider_and_institutional.json")["nasdaq_institutional_rows"]
    assert m.consistent_report_period(parse_iso_date(r["date"]) for r in rows).reason == "MIXED_REPORT_PERIODS"
    assert m.consistent_report_period([date(2026, 6, 30)] * 3).state is MetricState.OK
    assert m.consistent_report_period([date(2026, 6, 30), None]).state is MetricState.MISSING_INPUT
    # unit check: Nasdaq market_value is USD thousands (1.426B shares at ~USD 333 would otherwise be 4.75e11 USD)
    vanguard = rows[0]
    assert vanguard["market_value"] * 1000 / vanguard["shares_held"] == pytest.approx(333, rel=0.02)


def test_amended_filing_fixture_shapes():
    data = load("amendments_and_filings.json")
    assert {f["form"] for f in data["ten_k_a"]} == {"10-K/A"}
    assert "report_type" in data["filing_columns"] and "accession_number" in data["filing_columns"]
    assert "acceptance_datetime" not in data["filing_columns"]  # OpenBB drops it: point-in-time is date-granular
    assert data["form_type_filter_rejects"] == ["10-K/A", "10-Q/A"]  # amendments need a client-side filter
