# Phase 2: data-source and metric coverage audit

Status: **audit only**. No production ingestion, no screener, no AI agent. The machine-readable matrix is
[`data_coverage.yaml`](data_coverage.yaml) (the source of truth; the metric table at the bottom of this file is generated
from it and checked by `tests/test_data_coverage.py`). Executable metric rules: `app/screening/metrics.py`.

## 1. How to read the evidence

| Class | Meaning in this repository |
|---|---|
| `LIVE_VERIFIED` | A live call from a **GitHub-hosted runner** returned the data (runs cited by id in the YAML). |
| `API_SHAPE_VERIFIED` | Defined in the installed provider code / `obb.reference`; no live sample. |
| `FIXTURE_VERIFIED` | Verified only against a real response recorded by the OpenBB project's tests, at its recorded date. |
| `NOT_VERIFIED` | Believed possible, never tested. Unknown. |
| `UNAVAILABLE` | No suitable source among the installed providers / free public endpoints. |
| `DERIVED` | Computed locally from other entries; the rule is stated and, where possible, executable and tested. |

**What could not be done, and how it was handled.** The development sandbox cannot reach SEC, Nasdaq, Cboe, USAspending or any
news feed (network policy). No claim here is "live" because of a call from the sandbox. Live evidence comes from a read-only
probe (`scripts/audit/probe_providers.py`, workflow `.github/workflows/provider-probe.yml`) executed on GitHub Actions
(Linux, Python 3.14.7), recorded by run id:

* run `36954321217`: first probe, showed the SEC User-Agent behaviour (HTTP 403 without an email-shaped contact);
* run `36954761165`: all four groups (identity, market, fundamentals, ownership/events);
* run `36955401657`: the `followup` group (22/22 OK): multi-class market cap, company_type, net-income tags, NVDA split vintage,
  quarterly cash flow, dei shares.

Nothing was verified on **Windows** or against any paid provider (all four installed providers need no key). The 2026-10-02
values are a snapshot; they prove behaviour, not today's prices.

The Phase 2 request was cut off mid-sentence at "Review the Phase 1 SourceMetadat". The rest of the specification arrived later
and is covered in section 7. Phase 1 contained **no** class named `SourceMetadata` and **no** metric dictionary: the
provenance model is the `sources` table and `SourceRecord`, reviewed in section 7; the metric dictionary is created here
(YAML `metrics` + `comparability_rules`, executable in `app/screening/metrics.py`).

## 2. Providers inspected, installed, endpoints

Inspected (installed, hash-locked, unchanged from Phase 1): `openbb-core 2.0.1`, `openbb-sec 2.0.0`, `openbb-nasdaq 2.0.0`,
`openbb-cboe 2.0.0`, `openbb-news 2.0.0`. `obb.coverage.commands` lists 103 commands. **Newly installed providers: none.**
Dependency change: `PyYAML==6.0.3` declared directly in `requirements-dev.txt` (test/doc tooling only; it was already in the
runtime lock transitively; the dev lock was re-compiled).

Direct endpoints probed beyond OpenBB (because OpenBB hides fields we need): SEC `company_tickers_exchange.json`,
`submissions/CIK*.json`, `api/xbrl/companyfacts`, `api/xbrl/frames`, `efts` full-text search, Form 4 XML and 13F headers
(fixtures); Nasdaq `api.nasdaq.com` (through OpenBB), `nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt`; Cboe
`cdn.cboe.com` quote and history (through OpenBB); USAspending `api.usaspending.gov/api/v2` (recipient, awards, award detail,
transactions); Google News RSS and Yahoo Finance RSS.

## 3. Findings that change the design (each proven live)

1. **SEC statements carry no accession number or filing date per row.** OpenBB gives one wide row per period
   (latest vintage, or as-first-filed with `pit_mode=true`). Point-in-time and restatement handling therefore need the raw
   `companyfacts` points (`accn, filed, form, fy, fp, start, end, val`). OpenBB statements are a convenience, not the store.
2. **Per-share vintage trap (NVDA).** FY2024 diluted EPS is 1.19 by default (post 10:1 split) and 11.93 with `pit_mode`, while FY2023
   is 1.74 in both: naive growth is -31.6% for a company whose net income grew 581%. Rule: growth uses values from the **same
   filing** (`same_filing_pair`).
3. **`net_income` is not net income to common.** RIVN FY2025: `net_income` -3,626M (ProfitLoss, includes non-controlling interests)
   vs `net_income_to_common` -3,646M (NetIncomeLoss). The dictionary uses `net_income_to_common` for margins, EPS and P/E.
4. **TTM share counts are invalid.** AAPL TTM `weighted_ave_diluted_shares_os` is 59.1B (a four-quarter sum) against 14.6B outstanding.
5. **Absent is not zero.** Berkshire (`company_type` "diversified") shows no debt lines and no EPS on a USD 1.2T balance sheet. Assumed-zero
   debt applies only to `industrial` filers with a balance sheet (flagged); otherwise `MISSING_INPUT`.
6. **Multi-class issuers.** Nasdaq's per-quote `market_cap` is that class's price x ALL shares (GOOGL 4.137T, GOOG 4.096T). Summing
   listings double counts; the issuer value comes from one primary class. SEC `dei` shares-outstanding is absent for GOOGL and META
   and stale for BRK-B (2011), so shares for multi-class issuers need another route (implied from market cap / price).
7. **Ticker spelling differs by provider.** SEC `BRK-B`; Nasdaq and Cboe `BRK.B` (`BRK/B` on Nasdaq and `BRK-B` on Cboe quote fail). CIK is
   the identity; `app/models/symbols.py` converts.
8. **Prices are split-adjusted, not dividend-adjusted** (Cboe and Nasdaq identical on shared dates), so a split re-bases the whole history
   and `adj_close` must stay NULL. Cboe history reaches 2004; Nasdaq only about 10 years and rejects narrow windows.
9. **52-week high/low = intraday max/min over 365 days** (matches Nasdaq's published figure exactly for AAPL, NVDA, MSFT); it is computed, never stored.
10. **Insider data:** OpenBB returns the SEC's description text, not the code letter, and never fills `transaction_value`. Of 24 AAPL rows, only 8 were
    open-market sales (13 awards, 2 exercises, 1 tax withholding). 10b5-1 is not exposed.
11. **Institutional data:** Nasdaq mixes report dates (2025-12-31 and 2026-06-30 in one AAPL list), reports values in USD thousands and has no holder CIK. SEC 13F is per filer
    (CUSIPs, no ticker/CIK) and cannot be reversed to "who owns AAPL".
12. **Amendments:** OpenBB's `form_type` rejects `10-K/A`/`10-Q/A`; amended filings (AMD 10-K/A 2026-02-04, TSLA, old Google CIK) are found by a client-side filter on `report_type`.
13. **Ticker history is UNAVAILABLE** from every installed provider (only `formerNames` for names). Discontinued issuers (TWTR, ATVI) resolve by CIK with no ticker and no prices.
14. **USAspending cannot be matched to issuers by identifier** (no ticker/CIK; parent and child recipients with different UEIs).
15. **SEC requires a contact in the User-Agent.** A generic UA got HTTP 403; OpenBB's shipped placeholder worked. Production needs a real contact set by the operator.

## 4. Recommended source and fallback per dataset

| Dataset | Recommended source | Fallback |
|---|---|---|
| Issuer identity (CIK, name, SIC, fiscal year end) | SEC `company_tickers_exchange.json` + `submissions` | Nasdaq profile (no CIK) |
| Security type, exchange | nasdaqtrader symbol directory + SEC exchange | Nasdaq quote/profile |
| Share class | derived from SEC tickers per CIK + directory names | curated list for dual-class issuers |
| Daily prices | Cboe `equity.historical` (2004 on) | Nasdaq `equity.historical` (10 years) |
| Current quote | Nasdaq quote (cap, range, yield) + Cboe quote | last close from history |
| Benchmarks | Cboe ETF/index history | Nasdaq index history |
| Financial statements | SEC raw `companyfacts` (accn/filed preserved) | OpenBB `sec.income_statement/balance_sheet/cash_flow`; Nasdaq statements (USD thousands) |
| Cross-sectional screens | SEC XBRL `frames` | per-company `companyfacts` |
| Shares outstanding | SEC dei (single class) | Nasdaq cap / price |
| Filings index | SEC `submissions` (acceptance time, items) | OpenBB `company_filings` |
| Insider transactions | SEC Forms 3/4/5 via OpenBB | Nasdaq feed (no filing metadata) |
| Institutional | SEC 13F per filer (when a CUSIP map exists) | Nasdaq institutional (mixed periods) |
| Earnings dates / EPS surprise | Nasdaq calendar + historical EPS; 8-K Item 2.02 as the objective release record | none for guidance |
| News | Nasdaq company news | Google / Yahoo RSS (dedupe on normalised title + date) |
| Government awards | USAspending v2 (not ingested) | none |
| Events | 8-K item codes | SEC full-text search |

## 5. Rate limits and access

SEC documents 10 requests/second (OpenBB default 9, `OPENBB_SEC_REQUESTS_PER_SECOND`) and requires an identifying User-Agent.
Nasdaq and Cboe endpoints are unofficial, undocumented and unthrottled at audit volume (about 70 calls per group), with no SLA;
universe-scale behaviour is **NOT_VERIFIED**. Form 4 retrieval is one download per filing. A single AAPL `companyfacts` file is
3.8 MB. No API keys are needed or stored.

## 6. Metric dictionary and comparability rules

The dictionary is the YAML `metrics` list (72 entries: 41 LIVE_VERIFIED, 23 DERIVED, 2 API_SHAPE_VERIFIED, 1 FIXTURE_VERIFIED, 3 NOT_VERIFIED,
2 UNAVAILABLE) plus the executable rules in `app/screening/metrics.py`. Every derived metric returns `OK`, `MISSING_INPUT`,
`ZERO_DENOMINATOR`, `NOT_MEANINGFUL` or `NOT_COMPARABLE`; a screen treats anything but `OK` as "cannot be evaluated" and never as 0.

Comparability rules (YAML `comparability_rules`, C1 to C12): same period kind; periods about one year apart (a fiscal-year change is
`NOT_COMPARABLE`); same-filing vintage for growth; point-in-time by filing date; denominator sign rules (growth needs prior > 0 and
current >= 0, margins need revenue > 0, P/E needs EPS > 0, D/E needs equity > 0); missing is not zero (one flagged exception);
issuer vs listing; units and currency; TTM construction; mixed report periods; corporate actions that cannot be detected; amendments as new filings.

## 7. Source / evidence model review

Phase 1 stores provenance as `sources(provider, dataset, url, content_hash, detail, retrieved_at)`, append-only, referenced by a
nullable `source_id` on each fact table. It is **sufficient** for SEC facts and filings (accession, form, filed date are already
columns), for news (with a stable URL as `source_ref`) and, with a stopgap, for market data. It is **insufficient** for government
awards (no place for UEI/award id/modification/amount) and, for reproducibility, lacks fields the audit proved necessary:
structured `command` and `parameters` (the same command returned different data per `pit_mode`, `period`, symbol spelling and date
window), `provider_version`, and the provider's own `as_of` timestamp. These are recorded as required changes before Phase 3
(section 9); **nothing was migrated in this audit**. `tests/test_evidence_model.py` pins what the current model can and cannot hold.

## 8. Not verified / unavailable

* `UNAVAILABLE`: guidance, ticker history, GICS, delisting dates, structured share-class attribute.
* `NOT_VERIFIED`: sector-relative return (ETF mapping undefined), EBITDA (D&A definition unchecked), splits/dividend calendars
  (Nasdaq splits calendar returned zero rows for June 2024), universe-scale rate limits, the NYSE/other-listed symbol directory,
  preferred/unit/warrant ticker conventions, non-USD reporters, insurers/REITs/utilities statement templates.
* Shape only (`API_SHAPE_VERIFIED`): analyst price-target consensus, dividend history.
* Not verified anywhere: Windows behaviour of the probe or providers; behaviour for tickers other than the sampled ones.

## 9. Required before Phase 3

Schema (see YAML `source_evidence_model` and `schema_gaps`): add `sources.command`, `sources.parameters`, `sources.provider_version`,
`sources.as_of`; require `source_id` on ingested rows in the service layer; extend `ownership` with price, post-transaction shares,
acquired/disposed, derivative flag, security title, ownership nature, 10b5-1; keep `price_daily.adj_close` NULL; decide how share class and
ticker history are stored; defer an awards table and issuer-alias table until contracts are in scope.

Metric definitions changed by this audit (already applied in code and tests): `net_income` means attributable to parent; per-share growth requires the same filing;
`total_debt` assumed-zero applies only to `industrial`; TTM share counts are rejected; market cap is per issuer; returns are calendar-anchored and `MISSING_INPUT` when history is
short; ownership aggregation requires one report period.

## 10. Re-running the audit

Trigger the **Provider probe** workflow (manual `workflow_dispatch`, or any PR touching the probe). Optional repository variable
`SEC_USER_AGENT` (your name and a contact address) identifies the caller to the SEC. Output is one `PROBE {json}` line per probe in the job log.
After editing the YAML run `python tests/render_coverage.py`.

## 11. Metric table (generated)

<!-- BEGIN METRIC TABLE (generated by tests/render_coverage.py) -->

#### identity

| metric | status | source / command | direct or derived | live-tested |
|---|---|---|---|---|
| `issuer_cik` | LIVE_VERIFIED | sec: obb.sec.cik_map(symbol) | direct | yes: AAPL, BRK-B |
| `ticker_to_cik_universe_map` | LIVE_VERIFIED | sec: HTTP GET https://www.sec.gov/files/company_tickers_exchange.json | direct | yes: AAPL, GOOGL, GOOG, BRK-B, META, NVDA |
| `cik_to_ticker` | LIVE_VERIFIED | sec: obb.sec.symbol_map(query) | direct | yes: AAPL, BRK-B, GOOGL |
| `issuer_name_and_former_names` | LIVE_VERIFIED | sec: HTTP GET https://data.sec.gov/submissions/CIK##########.json (fields name, formerNames[name,from,to]) | direct | yes: META, TWTR, AAPL |
| `exchange_listing` | LIVE_VERIFIED | sec: HTTP GET https://www.sec.gov/files/company_tickers_exchange.json (exchange); obb.nasdaq.equity.quote (exchange | direct | yes: AAPL, BRK-B, KOSS |
| `security_type` | LIVE_VERIFIED | nasdaqtrader: HTTP GET https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt (columns Symbol, Security Name, Market C | direct | yes: AAPL, GOOG, GOOGL, META, NVDA, KOSS |
| `sic_code_and_description` | LIVE_VERIFIED | sec: HTTP GET https://data.sec.gov/submissions/CIK##########.json (sic, sicDescription) | direct | yes: AAPL, MSFT, NVDA, AMZN, GOOGL, META |
| `sector_industry_vendor` | LIVE_VERIFIED | nasdaq: obb.nasdaq.equity.quote(symbol) (sector, industry); obb.nasdaq.equity.screener | direct | yes: AAPL, GOOGL, GOOG, BRK.B, BRK.A |
| `fiscal_year_end` | LIVE_VERIFIED | sec: HTTP GET https://data.sec.gov/submissions/CIK##########.json (fiscalYearEnd as MMDD) | direct | yes: AAPL, MSFT, NVDA, AVGO, COST, PTON |
| `share_class` | DERIVED | derived: derived from SEC submissions tickers[] + nasdaqtrader Security Name | derived | yes: GOOGL, GOOG, META, BRK-B |
| `ticker_history` | UNAVAILABLE | none: none | direct | yes: META, TWTR, ATVI |
| `listing_status` | DERIVED | derived: derived: ticker present in company_tickers_exchange / submissions.tickers non-empty | derived | yes: TWTR, ATVI, GOOGL |

#### market

| metric | status | source / command | direct or derived | live-tested |
|---|---|---|---|---|
| `price_daily_ohlcv` | LIVE_VERIFIED | cboe: obb.cboe.equity.historical(symbol) | direct | yes: AAPL, AMZN, GOOGL, NVDA, MSFT, KO |
| `price_current_delayed` | LIVE_VERIFIED | nasdaq: obb.nasdaq.equity.quote(symbol); obb.cboe.equity.quote(symbol) | direct | yes: AAPL, NVDA, RIVN, BRK.B |
| `market_calendar` | LIVE_VERIFIED | nasdaq: obb.nasdaq.markets.status() | direct | yes: |
| `week52_high_low` | DERIVED | derived: derived; cross-check obb.nasdaq.equity.quote (year_high, year_low) | derived | yes: AAPL, NVDA, MSFT |
| `price_return` | DERIVED | derived: derived | derived | yes: RIVN |
| `drawdown_from_52w_high` | DERIVED | derived: derived | derived | yes: AAPL, NVDA, MSFT |
| `benchmark_return` | LIVE_VERIFIED | cboe: obb.cboe.equity.historical(SPY\|QQQ\|XLK) ; obb.cboe.index.historical(SPX) ; obb.nasdaq.index.historical(COMP) | direct | yes: SPY, QQQ, XLK, SPX, COMP |
| `relative_return_vs_benchmark` | DERIVED | derived: derived | derived | no |
| `sector_relative_return` | NOT_VERIFIED | derived: derived | derived | no |
| `shares_outstanding` | LIVE_VERIFIED | sec: HTTP GET https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json (dei:EntityCommonStockSharesOutstanding | direct | yes: AAPL, GOOGL, BRK-B, META |
| `market_cap_quote` | LIVE_VERIFIED | nasdaq: obb.nasdaq.equity.quote(symbol) (market_cap) | direct | yes: AAPL, NVDA, MSFT, GOOGL, GOOG, BRK.A |
| `market_cap_issuer` | DERIVED | derived: derived | derived | yes: GOOGL, GOOG, BRK.A, BRK.B |
| `average_volume_and_dividend_yield` | LIVE_VERIFIED | nasdaq: obb.nasdaq.equity.quote(symbol) (average_volume, annualized_dividend, dividend_yield, ex_dividend_date) | direct | yes: AAPL, RIVN, BRK.B |

#### income_statement

| metric | status | source / command | direct or derived | live-tested |
|---|---|---|---|---|
| `revenue` | LIVE_VERIFIED | sec: obb.sec.income_statement(symbol, period=annual\|quarterly\|ttm, limit, pit_mode, include_preliminary) -> total | direct | yes: AAPL, MSFT, NVDA, RIVN, PTON, COST |
| `cost_of_revenue` | LIVE_VERIFIED | sec: obb.sec.income_statement(...) -> total_cost_of_revenue | direct | yes: AAPL, RIVN, NVDA |
| `gross_profit` | LIVE_VERIFIED | sec: obb.sec.income_statement(...) -> total_gross_profit | direct | yes: AAPL, RIVN, NVDA |
| `operating_income` | LIVE_VERIFIED | sec: obb.sec.income_statement(...) -> total_operating_income | direct | yes: AAPL, RIVN, BRK-B, JPM |
| `net_income_attributable_to_parent` | LIVE_VERIFIED | sec: obb.sec.income_statement(...) -> net_income_to_common | direct | yes: RIVN, NVDA, BRK-B |
| `net_income_consolidated` | LIVE_VERIFIED | sec: obb.sec.income_statement(...) -> net_income | direct | yes: RIVN, NVDA |
| `diluted_eps` | LIVE_VERIFIED | sec: obb.sec.income_statement(...) -> diluted_eps | direct | yes: NVDA, AAPL, RIVN, BRK-B |
| `weighted_average_diluted_shares` | LIVE_VERIFIED | sec: obb.sec.income_statement(...) -> weighted_ave_diluted_shares_os | direct | yes: AAPL, RIVN |
| `ebitda` | NOT_VERIFIED | derived: obb.sec.income_statement(...) -> total_operating_income + depreciation_and_amortization | derived | yes: NVDA, GOOGL, RIVN |

#### cash_flow

| metric | status | source / command | direct or derived | live-tested |
|---|---|---|---|---|
| `operating_cash_flow` | LIVE_VERIFIED | sec: obb.sec.cash_flow(symbol, period=annual\|quarterly\|ttm) -> net_cash_from_operating_activities | direct | yes: AAPL, RIVN, KOSS, BRK-B |
| `capital_expenditures` | LIVE_VERIFIED | sec: obb.sec.cash_flow(...) -> purchase_of_plant_property_and_equipment (returned NEGATIVE) | direct | yes: AAPL |
| `free_cash_flow` | DERIVED | derived: derived | derived | yes: AAPL |

#### balance_sheet

| metric | status | source / command | direct or derived | live-tested |
|---|---|---|---|---|
| `cash_and_short_term_investments` | LIVE_VERIFIED | sec: obb.sec.balance_sheet(symbol, period) -> cash_and_equivalents, short_term_investments | direct | yes: AAPL, RIVN, KOSS, BRK-B |
| `debt_components` | LIVE_VERIFIED | sec: obb.sec.balance_sheet(...) -> short_term_debt, current_portion_of_long_term_debt, long_term_debt, capital_leas | direct | yes: AAPL, RIVN, KOSS, BRK-B |
| `company_type` | LIVE_VERIFIED | sec: obb.sec.income_statement(...).extra['results_metadata']['company_type'] | direct | yes: AAPL, GOOGL, NVDA, RIVN, BRK-B, JPM |
| `total_debt` | DERIVED | derived: derived | derived | yes: AAPL, BRK-B |
| `net_debt` | DERIVED | derived: derived | derived | yes: AAPL |
| `debt_to_equity` | DERIVED | derived: derived | derived | yes: AAPL |
| `total_equity` | LIVE_VERIFIED | sec: obb.sec.balance_sheet(...) -> total_common_equity (and total_equity) | direct | yes: AAPL, BRK-B, RIVN |
| `leverage_trend` | DERIVED | derived: derived | derived | no |

#### derived

| metric | status | source / command | direct or derived | live-tested |
|---|---|---|---|---|
| `revenue_growth_yoy` | DERIVED | derived: derived | derived | yes: NVDA, RIVN |
| `revenue_growth_ttm_yoy` | DERIVED | derived: obb.sec.income_statement(period=ttm) or sum of four discrete quarters | derived | yes: AAPL |
| `eps_growth_yoy` | DERIVED | derived: derived | derived | yes: NVDA, RIVN |
| `margins` | DERIVED | derived: derived | derived | yes: RIVN |
| `margin_change` | DERIVED | derived: derived | derived | yes: RIVN |
| `price_to_sales` | DERIVED | derived: derived | derived | no |
| `price_to_earnings` | DERIVED | derived: derived | derived | yes: RIVN |
| `comparability_year_over_year` | DERIVED | derived: derived | derived | no |

#### filings

| metric | status | source / command | direct or derived | live-tested |
|---|---|---|---|---|
| `filing_index` | LIVE_VERIFIED | sec: obb.sec.company_filings(symbol\|cik, form_type, limit) | direct | yes: AAPL, META, BRK-B, TWTR |
| `amended_filing_flag` | DERIVED | derived: client-side filter on company_filings report_type (OpenBB's form_type parameter rejects '10-K/A') | derived | yes: AMD, TSLA, AAPL |
| `filing_acceptance_datetime` | FIXTURE_VERIFIED | sec: HTTP GET https://data.sec.gov/submissions/CIK##########.json (filings.recent.acceptanceDateTime) | direct | no |
| `xbrl_fact_point` | LIVE_VERIFIED | sec: HTTP GET https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json ; https://data.sec.gov/api/xbrl/frames/ | direct | yes: AAPL, RIVN, KOSS, NVDA |

#### ownership

| metric | status | source / command | direct or derived | live-tested |
|---|---|---|---|---|
| `insider_transactions` | LIVE_VERIFIED | sec: obb.sec.insider_trading(symbol, start_date, end_date) | direct | yes: AAPL, NVDA |
| `insider_transaction_class` | DERIVED | derived: derived from transactionCode (raw) or OpenBB's description text | derived | yes: AAPL, NVDA |
| `institutional_holders_nasdaq` | LIVE_VERIFIED | nasdaq: obb.nasdaq.equity.ownership.institutional(symbol) | direct | yes: AAPL |
| `institutional_13f_holdings_sec` | LIVE_VERIFIED | sec: obb.sec.form_13f(symbol\|cik) ; obb.sec.institutions_search(query) | direct | yes: BRK-B |

#### earnings

| metric | status | source / command | direct or derived | live-tested |
|---|---|---|---|---|
| `earnings_calendar` | LIVE_VERIFIED | nasdaq: obb.nasdaq.equity.calendar.earnings(start_date, end_date) | direct | yes: AAPL, MSFT, NVDA |
| `earnings_eps_history` | LIVE_VERIFIED | nasdaq: obb.nasdaq.equity.fundamental.historical_eps(symbol) | direct | yes: AAPL, RIVN |
| `earnings_release_event` | DERIVED | sec: obb.sec.company_filings(form_type='8-K') filtered on items containing 2.02 | derived | yes: AAPL, MSFT, NVDA |
| `guidance` | UNAVAILABLE | none: none | direct | no |
| `analyst_price_target_consensus` | API_SHAPE_VERIFIED | nasdaq: obb.nasdaq.equity.estimates.consensus(symbol) | direct | no |

#### news

| metric | status | source / command | direct or derived | live-tested |
|---|---|---|---|---|
| `company_news` | LIVE_VERIFIED | nasdaq: obb.news.company(symbol, provider='nasdaq') ; HTTP GET https://news.google.com/rss/search?q=... ; Yahoo Financ | direct | yes: AAPL |

#### government

| metric | status | source / command | direct or derived | live-tested |
|---|---|---|---|---|
| `government_awards` | LIVE_VERIFIED | usaspending: HTTP POST https://api.usaspending.gov/api/v2/recipient/ ; /search/spending_by_award/ ; GET /awards/{id}/ ; POS | direct | yes: LMT, MSFT |

#### events

| metric | status | source / command | direct or derived | live-tested |
|---|---|---|---|---|
| `corporate_events_8k_items` | LIVE_VERIFIED | sec: obb.sec.company_filings(form_type='8-K') items column ; submissions filings.recent.items | direct | yes: AAPL, RIVN, PTON, META |
| `splits_dividends_calendar` | NOT_VERIFIED | nasdaq: obb.nasdaq.equity.calendar.splits ; obb.nasdaq.equity.calendar.dividend ; obb.nasdaq.equity.fundamental.divide | direct | yes: |
| `litigation_and_enforcement` | LIVE_VERIFIED | sec: obb.sec.rss_litigation() ; obb.sec.full_text_search(query) | direct | yes: |
| `dividend_history` | API_SHAPE_VERIFIED | nasdaq: obb.nasdaq.equity.fundamental.dividends(symbol) ; obb.nasdaq.equity.calendar.dividend(date) | direct | no |

<!-- END METRIC TABLE -->
