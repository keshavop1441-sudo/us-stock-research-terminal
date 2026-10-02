# Research methodology

Division of labour: **Python** computes and validates every figure; **Claude** understands the request, runs the workflow, explains validated results, synthesises qualitative evidence, and states what is unknown.

## Principles
1. **Deterministic numbers.** Financial facts, growth, margins, leverage, share counts, drawdowns, valuation, date and filing-period alignment, missing-data semantics and screening are computed by the engine. Claude never calculates or estimates them.
2. **Missing is explicit.** Missing, unavailable, unsupported-taxonomy, unreported and unreliable values are never converted to zero, a percentage or "low". Every metric carries a state: `OK`, `MISSING_INPUT`, `ZERO_DENOMINATOR`, `NOT_MEANINGFUL`, `NOT_COMPARABLE`. A screen treats any non-OK value as "cannot be evaluated".
3. **Provenance.** Facts keep their filing (accession, form, filing date, period, tag); market data keeps provider, command, retrieval time and content hash. Quote them.
4. **Known vs unknown.** Every answer separates engine-verified facts, web-sourced (unverified by the engine) items, and open questions.
5. **No scoring, no recommendations by default.** No "best stock" score or ranking; no buy/sell/hold unless the user explicitly asks.

## Financial methodology (do not relax)
* **Net income** = net income attributable to the parent/common (`NetIncomeLoss`), not consolidated `ProfitLoss`.
* **Growth** (revenue, EPS, FCF) takes both fiscal years from the **same filing**. Prior <= 0 or sign changes -> `NOT_MEANINGFUL`/`ZERO_DENOMINATOR`, never a percentage.
* **EPS** is compared on one filing's per-share basis (split-safe); TTM EPS has a split guard; **no TTM share counts** are ever built.
* **Margins** = line / revenue for the fiscal year; **margin change** and **leverage change** use values from one filing (else `NOT_COMPARABLE`).
* **Debt** is never inferred zero. `total_debt` needs short-term debt, current portion and long-term debt all reported (or explicit no-debt evidence); otherwise `MISSING_INPUT`. Operating leases excluded.
* **FCF** = operating cash flow - |capex|; absent capex -> `MISSING_INPUT`.
* **Market cap** is issuer-level (one per CIK): the provider-quoted cap of the issuer's designated primary listing. Multi-class issuers (GOOG/GOOGL, BRK-B) are never summed. Security-level market cap is unavailable.
* **Prices**: split-adjusted closes, not dividend-adjusted. Returns are **price returns** (never "total return"), anchored on calendar dates (1M, 3M, 6M, 1Y, 3Y, 5Y). **52-week drawdown** = last close / 52-week high - 1 (intraday high; closing-high variant also given).
* **Classification**: Nasdaq sector/industry labels and SEC SIC codes are separate taxonomies; a filter uses one; GICS is not used; values are never converted.
* **Symbols**: SEC `BRK-B` vs Nasdaq/Cboe `BRK.B` is handled by the engine; identity is the CIK.
* **Dates**: fiscal periods are identified by their own start/end dates (52/53-week years tolerated), never by `fy`/`fp` labels. Facts are as reported; amendments and restatements are separate rows; the current view uses the latest filed vintage and flags `RESTATED_PERIOD`.
* **IFRS / TSM**: foreign private issuers with only IFRS facts are `UNSUPPORTED_TAXONOMY`. Identity, prices and quotes work; every fundamental metric is `MISSING_INPUT`. No US-GAAP facts are invented.
* **Freshness**: price/quote metrics older than 7 days before the as-of date become `MISSING_INPUT` (`STALE_*`) unless explicitly allowed.

## Evidence standards for qualitative research
* Every web item: publisher, URL, publication date, what it says (quote or tight paraphrase). Prefer primary sources (SEC filings, company releases, court/regulator pages).
* Date-aware: state the event date and the retrieval date; flag anything older than the question's horizon.
* Separate fact ("the 8-K filed on D reports X") from inference ("this may relate to Y"); label inference.
* Event timing vs price movement: report both with dates; do not claim causality without a source that does.

## Scope limits (state them when relevant)
Only ingested securities are screened (the full market is not loaded). Insider transaction details, 13F holders, news, government awards and litigation text are not parsed by the engine. Live data needs network access to SEC, Nasdaq and Cboe hosts and a user-supplied SEC User-Agent contact.
