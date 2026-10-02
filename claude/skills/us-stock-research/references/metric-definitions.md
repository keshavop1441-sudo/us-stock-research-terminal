# Metric definitions

All metrics are computed from stored SEC facts and prices at read time (nothing derived is stored). `research.py catalog` is the authoritative list; this is the reading guide. Units: fraction = 0.15 means 15%.

## Growth (fraction; fiscal year, one filing for both years)
`revenue_growth_yoy`, `eps_growth_yoy` (diluted EPS, split-safe), `fcf_growth_yoy` (CFO and capex from one filing), `revenue_growth_ttm_yoy` (TTM vs TTM a year earlier; flagged when TTM components span filings).

## Profitability (fraction)
`gross_margin`, `operating_margin`, `net_margin` (net income to common / revenue). `gross_margin_change_yoy`, `operating_margin_change_yoy`, `net_margin_change_yoy`: margin minus prior-year margin in fraction points (positive = improving); inputs from one filing.

## Cash flow and earnings
`fcf` (USD, fiscal year), `revenue_ttm` (USD), `diluted_eps_ttm` (USD/share; sum of FY + YTD - prior YTD, flag `PER_SHARE_SUM_OF_PERIODS`).

## Balance sheet and leverage
`total_debt` (USD; current + non-current long-term debt are required, short-term debt is added when separately reported and flagged `SHORT_TERM_DEBT_NOT_REPORTED` otherwise; MISSING if a long-term line is absent), `net_debt` (USD; negative = net cash; short-term investments absent -> flagged zero), `debt_to_equity` (latest balance sheet; negative equity -> `NOT_MEANINGFUL`), `debt_to_equity_fy`, `debt_to_equity_prior_fy`, `debt_to_equity_change_yoy` (positive = leverage rose; "not worsening" = <= 0), `shares_outstanding` (cover-page count, single-class and recent only).

## Valuation (issuer level)
`issuer_market_cap` (provider-quoted, primary listing), `price_to_sales` (market cap / latest fiscal-year revenue), `price_to_sales_ttm`, `price_to_earnings` (last close / TTM EPS; never for losses; multi-class flagged `MULTI_CLASS_PER_SHARE_BASIS_UNVERIFIED`).

## Price (per listing; price returns only)
`price_return_1m/3m/6m/1y/3y/5y`, `drawdown_from_52w_high` (<= 0), `drawdown_from_52w_closing_high`, `high_52w`, `high_52w_close`.

## States
| State | Meaning |
|---|---|
| `OK` | value usable |
| `MISSING_INPUT` | an input is absent, conflicting, unsupported or stale; see `reason` |
| `ZERO_DENOMINATOR` | divisor exactly zero (e.g. prior revenue 0) |
| `NOT_MEANINGFUL` | defined but misleading (negative base, loss-making P/E, negative equity) |
| `NOT_COMPARABLE` | inputs not comparable (different periods/filings, split basis change, restatement) |

Common `reason` codes: `NO_FILING_REPORTS_BOTH_YEARS`, `TAG_CONFLICT:a!=b`, `DEBT_COMPONENT_ABSENT:...`, `UNSUPPORTED_TAXONOMY:ifrs-full`, `STALE_PRICE_DATA:last_bar=...`, `SIGN_CHANGE`, `NEGATIVE_BASE`, `NEGATIVE_EARNINGS`, `MARGIN_INPUTS_FROM_DIFFERENT_FILINGS`, `POSSIBLE_SPLIT_BASIS_CHANGE`, `TTM_PERIODS_MISALIGNED`.

Common `flags` (advisory on an OK value): `RESTATED_PERIOD`, `MIXED_FILINGS`, `COMPOSED:FY+YTD-YTD_PRIOR`, `FINANCE_LEASES_NOT_REPORTED`, `ASSUMED_ZERO:short_term_investments`, `QUOTE_STALE:nD`, `MULTI_CLASS_ALL_SHARES_AT_PRIMARY_PRICE`.
