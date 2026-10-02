"""The screenable metrics: the ONLY names a screen specification may refer to.

Every entry is computed by ``app.screening`` (snapshot.py/metrics.py, defined in docs/data_coverage.yaml). This file
adds no formula: it names what exists, says at which level it lives (issuer or listing), in which unit its value is
stored, and where its inputs come from, so a screen specification can be validated before anything runs.

Units (the unit of the STORED value; a threshold is compared in these units):
  fraction   0.15 means 15%  (growth, margins, margin changes, price returns, drawdowns). Thresholds in a screen
             specification must say ``unit: "fraction"`` or ``unit: "percent"`` explicitly - a bare 15 for "15%" is the
             classic ten-fold error and is rejected.
  ratio      a plain multiple (P/S 5.0, debt/equity 0.8, debt/equity change +0.1)
  usd        US dollars (an absolute amount)
  usd_per_share, shares
"""

from dataclasses import dataclass
from typing import Literal

Scope = Literal["issuer", "listing"]
Unit = Literal["fraction", "ratio", "usd", "usd_per_share", "shares"]
FRACTION: Unit = "fraction"


@dataclass(frozen=True)
class MetricSpec:
    name: str
    scope: Scope
    unit: Unit
    description: str
    lines: tuple[str, ...] = ()  # normalised accounting lines whose provenance (filing, tag) backs the value
    uses_prices: bool = False


def _m(name: str, scope: Scope, unit: Unit, description: str, lines: tuple[str, ...] = (), prices: bool = False):
    return MetricSpec(name, scope, unit, description, lines, prices)


_METRICS = (
    # growth (fiscal year, both years from ONE filing)
    _m(
        "revenue_growth_yoy",
        "issuer",
        "fraction",
        "Latest fiscal year revenue / prior year - 1 (same filing).",
        ("revenue",),
    ),
    _m(
        "eps_growth_yoy",
        "issuer",
        "fraction",
        "Diluted EPS growth, both years from the same filing (split-safe).",
        ("diluted_eps",),
    ),
    _m(
        "fcf_growth_yoy",
        "issuer",
        "fraction",
        "FCF growth; operating cash flow and capex for both years from one filing.",
        ("operating_cash_flow", "capex"),
    ),
    _m(
        "revenue_growth_ttm_yoy",
        "issuer",
        "fraction",
        "Trailing-twelve-month revenue vs the TTM one year earlier.",
        ("_ttm_revenue", "_ttm_revenue_prior"),
    ),
    # profitability
    _m(
        "gross_margin", "issuer", "fraction", "Gross profit / revenue, latest fiscal year.", ("gross_profit", "revenue")
    ),
    _m(
        "operating_margin",
        "issuer",
        "fraction",
        "Operating income / revenue, latest fiscal year.",
        ("operating_income", "revenue"),
    ),
    _m(
        "net_margin",
        "issuer",
        "fraction",
        "Net income to common / revenue, latest fiscal year.",
        ("net_income_to_common", "revenue"),
    ),
    _m(
        "gross_margin_change_yoy",
        "issuer",
        "fraction",
        "Gross margin minus prior-year gross margin (fraction points; positive = improving).",
        ("gross_profit", "revenue"),
    ),
    _m(
        "operating_margin_change_yoy",
        "issuer",
        "fraction",
        "Operating margin minus prior-year (fraction points; positive = improving).",
        ("operating_income", "revenue"),
    ),
    _m(
        "net_margin_change_yoy",
        "issuer",
        "fraction",
        "Net margin minus prior-year (fraction points; positive = improving).",
        ("net_income_to_common", "revenue"),
    ),
    # cash flow, levels
    _m(
        "fcf",
        "issuer",
        "usd",
        "Free cash flow = operating cash flow - capex, latest fiscal year (USD).",
        ("operating_cash_flow", "capex"),
    ),
    _m("revenue_ttm", "issuer", "usd", "Trailing-twelve-month revenue (USD).", ("_ttm_revenue",)),
    _m(
        "diluted_eps_ttm",
        "issuer",
        "usd_per_share",
        "Trailing-twelve-month diluted EPS (per-share sum, split-guarded).",
        ("_ttm_diluted_eps",),
    ),
    # balance sheet / leverage
    _m(
        "total_debt",
        "issuer",
        "usd",
        "Short-term + current long-term + long-term debt; MISSING unless all three are reported.",
        ("short_term_debt", "current_portion_long_term_debt", "long_term_debt"),
    ),
    _m(
        "net_debt",
        "issuer",
        "usd",
        "Total debt - (cash + short-term investments); negative = net cash.",
        ("short_term_debt", "current_portion_long_term_debt", "long_term_debt", "cash", "short_term_investments"),
    ),
    _m(
        "debt_to_equity",
        "issuer",
        "ratio",
        "Total debt / stockholders' equity at the latest balance sheet.",
        ("short_term_debt", "current_portion_long_term_debt", "long_term_debt", "equity"),
    ),
    _m(
        "debt_to_equity_fy",
        "issuer",
        "ratio",
        "Debt/equity at the latest fiscal year end (one annual filing).",
        ("equity",),
    ),
    _m(
        "debt_to_equity_prior_fy",
        "issuer",
        "ratio",
        "Debt/equity one fiscal year earlier, from the same annual filing.",
        ("equity",),
    ),
    _m(
        "debt_to_equity_change_yoy",
        "issuer",
        "ratio",
        "Debt/equity change over the fiscal year (positive = leverage ROSE; 'not worsening' = <= 0).",
        ("equity",),
    ),
    _m(
        "shares_outstanding",
        "issuer",
        "shares",
        "Cover-page share count; only for single-class issuers with a recent value.",
        ("shares_outstanding",),
    ),
    # valuation (issuer-level market cap = primary listing's quoted cap)
    _m(
        "issuer_market_cap",
        "issuer",
        "usd",
        "Provider-quoted market cap of the issuer's designated primary listing.",
        (),
        True,
    ),
    _m("price_to_sales", "issuer", "ratio", "Issuer market cap / latest FISCAL-YEAR revenue.", ("revenue",), True),
    _m("price_to_sales_ttm", "issuer", "ratio", "Issuer market cap / TTM revenue.", ("_ttm_revenue",), True),
    # price metrics (per listing; PRICE returns, no dividends)
    _m(
        "price_return_1m",
        "listing",
        "fraction",
        "Price return over 1 month (calendar-date anchored, split-adjusted close).",
        (),
        True,
    ),
    _m("price_return_3m", "listing", "fraction", "Price return over 3 months.", (), True),
    _m("price_return_6m", "listing", "fraction", "Price return over 6 months.", (), True),
    _m("price_return_1y", "listing", "fraction", "Price return over 1 year.", (), True),
    _m("price_return_3y", "listing", "fraction", "Price return over 3 years.", (), True),
    _m("price_return_5y", "listing", "fraction", "Price return over 5 years.", (), True),
    _m(
        "drawdown_from_52w_high",
        "listing",
        "fraction",
        "Last close / 52-week intraday high - 1 (<= 0; -0.35 = 35% below the high).",
        (),
        True,
    ),
    _m(
        "drawdown_from_52w_closing_high",
        "listing",
        "fraction",
        "Last close / 52-week closing high - 1 (<= 0).",
        (),
        True,
    ),
    _m("high_52w", "listing", "usd_per_share", "52-week intraday high.", (), True),
    _m("high_52w_close", "listing", "usd_per_share", "52-week closing high.", (), True),
    _m(
        "price_to_earnings",
        "listing",
        "ratio",
        "Last close / TTM diluted EPS; never produced for losses.",
        ("_ttm_diluted_eps",),
        True,
    ),
)

CATALOG: dict[str, MetricSpec] = {spec.name: spec for spec in _METRICS}
assert len(CATALOG) == len(_METRICS), "duplicate metric name in the catalog"


def catalog_json() -> list[dict[str, object]]:
    """The catalog as plain data (what ``research.py catalog`` prints for Claude)."""
    return [
        {
            "metric": s.name,
            "scope": s.scope,
            "unit": s.unit,
            "description": s.description,
            "input_lines": list(s.lines),
            "uses_prices": s.uses_prices,
        }
        for s in CATALOG.values()
    ]
