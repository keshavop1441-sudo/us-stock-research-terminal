---
name: us-stock-research
description: Evidence-based US stock research with deterministic data and calculations. Use for US stock screening (growth, margins, free cash flow, leverage, valuation, price drawdowns), financial metric analysis, company research from SEC filings (10-K, 10-Q, 8-K, earnings), insider and ownership filings, news and events, contracts, partnerships, M&A, legal and regulatory developments, "why did this stock fall?" investigations, and comparisons of US-listed companies. Python computes every number from SEC and exchange data and returns validated JSON evidence; Claude then explains it, separating what is known from what is unknown. Do not use for non-US markets, crypto, portfolio management, trading, price predictions or general finance questions.
---

# US Stock Research

Claude is the researcher and narrator. **Python is the source of truth for every number.** Run `scripts/research.py`; read its JSON; never compute, estimate or "round into" a financial figure yourself, and never fill a gap with remembered data.

## When to use / not use

Use it for: screening US stocks; metrics (growth, margins, FCF, leverage, valuation, returns, drawdowns); researching a company; filings, earnings, insiders, ownership; events, contracts, partnerships, M&A, legal/regulatory; "why did X fall?"; comparing companies.

Do **not** use it for: non-stock questions; purely conversational or conceptual finance questions ("what is a P/E ratio?"); non-US issuers' fundamentals (IFRS filers such as TSM have no fundamentals here); buy/sell advice, price targets, forecasts, portfolio or trading tasks. Answer those normally, without the scripts.

## Setup check (once per session)

Run every command from this Skill's folder. A fresh environment has the bundled engine but **no Python packages**; install them from the hash-checked lock file, never from `requirements.txt` (direct pins only) and never unpinned.

1. `python scripts/research.py doctor --network` (add `--pretty`). It reports what works here. Never print secrets.
2. If the output is a JSON error with `errors[0].code` = `DEPENDENCIES_MISSING` (or a `ModuleNotFoundError`), run once: `python -m pip install --require-hashes -r scripts/requirements.lock` (the same text is in `data.fix_command`), then repeat step 1. Do not install anything else, do not drop `--require-hashes`, and do not edit the lock. This needs PyPI access; if pip cannot reach it (for example the API sandbox has no network), tell the user what is blocked and stop short of guessing.
3. Python: the project targets 3.14 (`.python-version`); the engine also runs on 3.11 and newer (checked on 3.12). Use the interpreter that is there. Below 3.11, tell the user instead of continuing.
4. Live SEC data needs `SEC_USER_AGENT` = `<ApplicationName> <contact email or URL>`. If it is not configured, **ask the user for their own** and pass `--sec-user-agent "..."` (or set the env var). Never invent or reuse a contact.
5. Needs outbound HTTPS to `www.sec.gov`, `data.sec.gov`, `api.nasdaq.com`, `cdn.cboe.com`; if `doctor` shows a host unreachable, tell the user (see `references/sources-coverage.md`) instead of improvising another source.
6. Data is stored in a local DuckDB file (`DATABASE_PATH`, default `./research_data/research.duckdb`).

## Commands (all print one JSON envelope: `status`, `data`, `warnings`, `errors`)

| Need | Command |
|---|---|
| Supported metrics + screen schema | `catalog` |
| Check a screen translation | `validate-screen --spec-json '{...}'` |
| Ticker -> CIK/name | `resolve --symbols A B` |
| Fetch + store data (max 25 symbols/call) | `ingest --symbols A B [--stages prices quotes facts]` |
| What is stored | `universe` |
| Canonical metrics + provenance | `metrics --symbols A B` |
| Deterministic screen | `screen --spec-json '{...}' [--ingest-missing]` |
| Evidence packet (one company) | `evidence --symbol A` |
| SEC filing events, no storage | `events --symbols A B` |

Details, arguments, status codes: `references/cli-reference.md`.

## Choose the workflow (full steps in `references/workflows.md`)

- **A. Screen**: translate the request into a screen spec (`references/screen-translation.md`), `validate-screen`, make sure candidates are ingested, `screen`. Python filters; you only translate and explain.
- **B. Metric analysis**: `ingest` if needed, `metrics` (or `evidence`), explain values with their states and flags.
- **C. Company research**: `ingest`, `evidence`, then fill gaps with attributed web research.
- **D. "Why did it fall?"**: `evidence` (price context, drawdown, fundamentals, dated filings) + web research for dated catalysts; present competing explanations, never a single asserted cause.
- **E. Comparison**: `metrics`/`evidence` for each company on the same `--as-of`; compare only metrics that are OK for all; list the rest as missing.
- **F-H. Events, contracts/partnerships/M&A, legal/regulatory**: `events`/`evidence` sections first (filing-level facts), then web research with source, URL and publication date.

## Hard rules

1. **Numbers come from the scripts.** Quote `value` only when `state` is `OK`; otherwise say "N/A" with the `state`/`reason`. Missing, unavailable, unsupported, unreported and unreliable are never zero.
2. Respect `flags` and `caveats` (e.g. `MULTI_CLASS_PER_SHARE_BASIS_UNVERIFIED`, `STALE_*`, `RESTATED_PERIOD`). Price returns exclude dividends; never call them total return.
3. TSM and other IFRS issuers: fundamentals are `UNSUPPORTED_TAXONOMY`; identity, prices and quotes still work. Never fake US-GAAP figures.
4. A screen has no score and no ranking; list results alphabetically and show exclusions with reasons. If the universe is small, say the screen covers **only what has been ingested**, not the market.
5. Web research is for qualitative events only: cite publisher, URL and date for every item, label it unverified by the engine, and keep it separate from engine numbers. Dated, not speculative: say "followed", not "caused", unless the source says so.
6. Mark every section as known / unknown. No buy/sell/hold recommendations unless the user explicitly asks, and then only as a framework with the evidence limits stated.
7. Respect stale-data blocking (`--max-price-age-days`); if prices are stale, re-run `ingest --stages prices quotes` rather than using `--allow-stale` silently.
8. Never bypass the scripts to scrape or call data sources directly for structured financials.

## Output

Use the layouts in `references/output-templates.md` (screen / company / why-did-it-fall / comparison). Be concise but evidence-rich: figures with period, filing date and accession where the JSON provides them, then a Sources list.
