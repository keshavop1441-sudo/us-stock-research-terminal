# Output templates

Concise but evidence-rich. Every figure comes from the JSON (value + period/as-of). Use "N/A (state: reason)" for non-OK metrics. End with **Sources**: filings (form, filing date, accession), engine retrievals (provider, retrieved_at), web items (publisher, URL, date). Label web items "not verified by the engine". No buy/sell/hold unless asked.

## 1. Screen
1. **Screen definition**: the criteria exactly as run (metric, operator, threshold, unit), taxonomy, as-of date, universe scope ("N ingested securities; the full market is not loaded").
2. **Matches**: table, alphabetical (no ranking): ticker, name, one column per criterion with exact value.
3. **Why each matched**: per match, each criterion value vs threshold; mention flags.
4. **Exact metrics**: filing/period basis (fiscal year end, accession) for fundamentals; last bar/quote date for price metrics.
5. **Failed / missing or excluded data**: counts; failed securities in one line each; every missing-data exclusion with metric, state, reason.
6. **Sources** and limits (stale prices, unsupported taxonomy, undesignated primary listing).

## 2. Company research
**Company snapshot** (identity, classification with taxonomy, listings) -> **Financial state** (growth, margins, cash flow, with fiscal year and filing) -> **Valuation** (market cap, P/S, P/E with caveats) -> **What changed** (vs prior year, from the metrics) -> **Recent catalysts/events** (dated; filings first, then attributed web items) -> **Contracts/partnerships** -> **M&A** -> **Legal/regulatory** -> **Insider/institutional activity** (filing-level only; say details are not parsed) -> **Key risks and unknowns** (explicit list: engine gaps, missing metrics, unverified web claims) -> **Sources**.
Sections with nothing: write "None found in <source, window>" or "Not collected".

## 3. "Why did this stock fall?"
**Price-performance context** (returns, drawdown, 52-week range, last close, dates) -> **Dated catalysts** (timeline) -> **Earnings/guidance** -> **Fundamentals** (what changed, which filing) -> **Valuation changes** -> **Macro/sector factors** (attributed) -> **Company-specific events** -> **Evidence linking event timing to price movement** (state what the dates show; say when no daily-move evidence exists) -> **Competing explanations** (each with support and weakness) -> **Unresolved questions** -> **Sources**. Never present speculation as established fact.

## 4. Comparison
Table with one row per metric and one column per company, same as-of date; only metrics OK for all go in the table; a **Missing** list per company with reasons; notes on multi-class, IFRS and fiscal-year differences; qualitative differences from evidence packets; **Sources**. No winner score.

## 5. Events / contracts / M&A / legal
Dated list (newest first): date, event, source type (SEC filing / web), what the source says, what is not established. Group by section; end with unknowns and **Sources**.
