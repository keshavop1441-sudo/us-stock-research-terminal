# Translating a request into a screen specification

You translate; Python filters. Always run `validate-screen` before `screen`, and tell the user the exact criteria you used.

## Shape

```json
{"name": "short label",
 "as_of": "2026-01-31",
 "universe": {"symbols": ["AAPL", "MSFT"]},
 "classification": {"taxonomy": "nasdaq", "sector": "Technology"},
 "criteria": [{"metric": "revenue_growth_yoy", "op": "gt", "value": 15, "unit": "percent"}]}
```

* `op`: `gt gte lt lte eq between` (`between` takes `low`/`high`, inclusive). Metrics: run `catalog`.
* `unit` is **required** for fraction metrics (growth, margins, margin changes, returns, drawdowns): `percent` (15 = 15%) or `fraction` (0.15). For other metrics use `native` (the default).
* `universe.symbols` is optional; without it every stored security is screened. The market is **not** loaded: screens cover ingested securities only.
* Classification uses ONE taxonomy: `nasdaq` (`sector`, `industries`, `exclude_industries`; raw Nasdaq labels) or `sec_sic` (`sic_codes`). They are never converted; GICS is unsupported. Securities without that classification are reported as missing-data exclusions, not matches.

## Phrase -> criterion

| User says | Criterion |
|---|---|
| "down 30-50% from the 52-week high" | `drawdown_from_52w_high` between -50 and -30, percent |
| "revenue growth > 15%" | `revenue_growth_yoy` gt 15 percent |
| "EPS growth > 15%" | `eps_growth_yoy` gt 15 percent |
| "positive free cash flow" | `fcf` gt 0 (native, USD) |
| "P/S under 5" | `price_to_sales` lt 5 (fiscal-year revenue) or `price_to_sales_ttm` |
| "improving margins" | `gross_margin_change_yoy` / `operating_margin_change_yoy` gt 0 fraction (say which) |
| "leverage not worsening" | `debt_to_equity_change_yoy` lte 0 |
| "profitable" | `net_margin` gt 0 percent |
| "up over the past year" | `price_return_1y` gt 0 fraction |
| "tech" | classification nasdaq sector `Technology` (state that it is Nasdaq's label) |

## Rules

* Pick the closest catalogued metric and **say so** if the wording is looser than the metric ("improving margins" has several readings: choose, state, or ask once).
* If no metric exists (e.g. dividend yield, analyst ratings, ROIC), say it is unsupported; do not approximate.
* Never add a score or a "best" ranking. Report the three result groups: matches, failed, missing-data exclusions.
* To screen candidates you propose, `ingest` them first (or `--ingest-missing`, max 25).
