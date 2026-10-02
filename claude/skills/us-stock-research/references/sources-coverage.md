# Sources and coverage

## Structured data (audited, public, no API keys, no paid sources)
| Data | Source | Notes |
|---|---|---|
| Identity, CIK, filings index | SEC `company_tickers_exchange.json`, `data.sec.gov/submissions` | identity authority; CIK is the issuer key |
| Accounting facts | SEC `data.sec.gov/api/xbrl/companyfacts` (us-gaap, dei) | raw as-reported points per filing; provenance layer |
| Daily prices | Cboe (OpenBB `obb.cboe.equity.historical`), Nasdaq fallback | split-adjusted, not dividend-adjusted |
| Quotes, market cap, 52-week range, raw sector/industry | Nasdaq (OpenBB `obb.nasdaq.equity.quote`) | provider-quoted cap per listing |
| Filing events | SEC submissions (`items` of 8-Ks, form types) | classified by form and 8-K item code only |

SEC access requires an identifying User-Agent (`SEC_USER_AGENT="<ApplicationName> <contact email or URL>"`, the user's own contact) and is rate limited (about 8 requests/s ceiling; HTTP 403 is never retried). Network hosts needed: `www.sec.gov`, `data.sec.gov`, `api.nasdaq.com`, `cdn.cboe.com`. If the environment blocks them, `doctor --network` shows which; nothing is fetched another way.

## Evidence verification levels
Provider behaviour was audited live on 2026-10-02 (see `docs/data_coverage.yaml` in the repository: `LIVE_VERIFIED`, `API_SHAPE_VERIFIED`, `FIXTURE_VERIFIED`, `NOT_VERIFIED`, `UNAVAILABLE`, `DERIVED`). Some standard us-gaap tags (e.g. `GrossProfit`, `OperatingIncomeLoss`, `StockholdersEquity`, cash, short-term debt) are used per the standard taxonomy but have not each been individually probed live; a missing line simply yields `MISSING_INPUT`.

## Supported
US-listed issuers with US-GAAP XBRL facts (10-K/10-Q); prices, quotes, price metrics for listed symbols; filing-level events; screens over ingested securities; multi-class issuers (issuer-level market cap via the primary listing; first-loaded listing if undesignated, shown in `cross_checks.primary_listing_designation`).

## Not supported / not collected
* IFRS or foreign private issuer fundamentals (TSM): `UNSUPPORTED_TAXONOMY`.
* Total return, dividends-adjusted prices, security-level market cap, benchmark-relative returns (not produced by the engine), analyst estimates, ratings, ROIC, dividend yield.
* Insider transaction details (price/shares/buy-sell), 13F institutional holders and percentages: filings are listed, not parsed.
* News, press releases, customer wins, partnerships, government awards (USAspending), litigation text: not collected; use web research with attribution.
* The full US market universe: not loaded; screens cover ingested securities. Ingest is capped at 25 symbols per call.
* Non-US markets, crypto, options, futures.

## Web research rules (qualitative only)
Allowed for news, events, contracts, partnerships, M&A rumours/announcements, legal and regulatory developments, macro and sector context. Require publisher, URL, publication date; prefer primary sources (SEC, company IR, court/regulator). Never use web pages to replace structured financial values from the engine; if the web and the engine disagree, report both and the filing-based figure.
