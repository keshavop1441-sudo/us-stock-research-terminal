# Workflows

Always: `doctor` first in a new session; use one `--as-of` date throughout a comparison.

## A. Deterministic screen
1. Translate (`screen-translation.md`) -> `validate-screen`.
2. `universe` to see what is stored. If candidates are missing: `ingest --symbols ...` (<=25 per call; repeat in batches) or `screen --ingest-missing`.
3. `screen`. Report: screen definition (with units), matches (exact values, thresholds, flags), failed (brief), missing-data exclusions with reasons, universe scope (ingested only), sources.
4. Do not rank. Offer to run `evidence` on selected matches.

## B. Metric analysis
`ingest` if not stored -> `metrics --symbols ...` -> explain each value with `state`, `flags` and its filing (`lines[*].accession/form/filed/period_end`). Growth uses one filing for both years; EPS growth is split-safe; net income = to common; debt is never inferred zero.

## C. Company research
`ingest` -> `evidence --symbol X`. Read all sections: use items as facts, `missing` and `NOT_COLLECTED` as unknowns. Then web research for news, customers, partnerships, litigation, government awards (see `sources-coverage.md`). For each web item record publisher, URL, publication date. For filing-based items (8-K, Form 4, 13D/G) open the filing before characterising it: the engine only reports form, date and item codes.

## D. "Why did this stock fall?"
1. `ingest --stages prices quotes` (fresh), `evidence`: price returns, drawdown, 52-week range, last close and dates.
2. Fundamentals and valuation: what changed (growth, margins, leverage, valuation) and in which filing.
3. Dated catalysts: `evidence` filing events (earnings 8-K 2.02, 8-K items, insider filings) plus web research for guidance, analyst/sector news, macro events. Build a dated timeline.
4. Link timing to price only with the engine's price data (dates, returns). If you have no daily-move evidence for a date, say the link is unestablished.
5. Present competing explanations and the unresolved questions. No single asserted cause.

## E. Comparison
`ingest` all, `metrics`/`evidence` with the same `--as-of`. Table only metrics that are OK for every company; list the rest per company with reasons. Mention multi-class and IFRS caveats. No overall winner score.

## F. Recent events
`events --symbols ...` (live filings) or the `evidence` filing sections; web research for news. Order by date, cite sources.

## G. Contracts / partnerships / M&A
8-K Items 1.01/1.02 and merger/tender forms appear in `contracts_customers` / `mna`: they say *an agreement or transaction was filed*, not what it is. Read the filing, then add web sources. `partnerships` and `government_awards` are web-research sections.

## H. Legal / regulatory
`legal_regulatory` holds filing-level signals (bankruptcy, cyber incidents, delisting notices, non-reliance). Litigation described in 10-K/10-Q text is not parsed: read the filing or use attributed web research.

## When data is missing
Say what is missing and why (`state`/`reason`), what you could do (e.g. `ingest`, user supplies SEC_USER_AGENT, enable network access), and stop short of estimating.
