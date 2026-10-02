# US Stock Research (Claude Skill)

A **Claude-native US stock research system**. You work in a Claude Project; the **us-stock-research** Skill runs deterministic Python/data workflows over public SEC, Nasdaq and Cboe data; Claude explains the validated results and adds qualitative research. This repository is the source-controlled implementation and methodology of that Skill.

```
Claude Project -> you type a research request -> Claude invokes the Skill
  -> Python retrieves public data, computes every metric, filters screens
  -> Claude receives validated JSON evidence -> concise, evidence-rich answer with sources
```

**The LLM is never the source of truth for numbers.** Python owns financial facts, growth, margins, leverage, share counts, drawdowns, valuation, date/filing-period alignment, missing-data semantics and screening. Claude translates requests, explains results, synthesises qualitative evidence and says what is unknown. Missing, unavailable, unsupported, unreported or unreliable data is never turned into zero.

> **Migration note.** This project used to be a local Streamlit + Ollama (qwen/deepseek) research terminal for Windows. That architecture is retired: the Streamlit app, launchers, local-LLM provider and their tests and dependencies were removed (see [`docs/architecture.md`](docs/architecture.md)). The data engine, DuckDB store, provenance, metric methodology and the P0 ingestion pilot were kept and now serve the Skill. No local model, hosted web app or paid API is needed.

## What the Skill does

| Workflow | Python (deterministic) | Claude |
|---|---|---|
| **Screen** | filters ingested securities with exact thresholds; per-criterion PASS/FAIL/missing; no score, no ranking | translates the request to a strict screen spec; explains matches and exclusions |
| **Metric analysis** | growth, margins, FCF, leverage, valuation, returns, drawdowns with provenance | explains values, states, flags |
| **Company research** | evidence packet: identity, price, fundamentals, valuation, balance sheet, filings, earnings, insider/ownership filings, event sections | adds attributed web research (news, contracts, partnerships, litigation) and the narrative |
| **"Why did it fall?"** | price context, drawdown, fundamentals, dated filings | dated timeline, competing explanations, unresolved questions |
| **Comparison** | same-date metrics for several companies | compares only what is computable for all |

Supported data: SEC (identity, filings, XBRL company facts, filing events), Cboe daily prices (Nasdaq fallback), Nasdaq quotes/market cap/raw sector. Screenable metrics: run `research.py catalog` (revenue/EPS/FCF growth, gross/operating/net margin and margin change, FCF, debt, net debt, debt/equity and its change, issuer market cap, P/S, P/E, 1M-5Y price returns, 52-week drawdowns) with Nasdaq-sector or SEC-SIC classification filters.

**Not supported / not collected:** IFRS/foreign-issuer fundamentals (e.g. TSM: identity, prices and quotes only), total return, analyst estimates, dividend yield, insider transaction details and 13F holders (filings are listed, not parsed), news/contracts/partnerships/government awards/litigation text (Claude uses attributed web research), non-US markets, trading, portfolios, predictions, recommendations. **Screens cover only securities that have been ingested**; the full market universe is deliberately not loaded (see `docs/data_coverage.yaml`, `universe_pilot`).

## Install and use (GitHub repository -> working Claude Project)

Full guide: [`claude/project/SETUP.md`](claude/project/SETUP.md). In short:

1. `git clone https://github.com/keshavop1441-sudo/us-stock-research-terminal.git && cd us-stock-research-terminal`
2. `python scripts/package_skill.py` -> `dist/us-stock-research.zip` (standard library only; reproducible).
3. In Claude: enable code execution; upload the ZIP under Skills; allow network access to `www.sec.gov`, `data.sec.gov`, `api.nasdaq.com`, `cdn.cboe.com`.
4. Create a Project; paste [`claude/project/project-instructions.md`](claude/project/project-instructions.md) into "Set project instructions"; add the four files from `claude/skills/us-stock-research/references/` (`research-methodology.md`, `metric-definitions.md`, `sources-coverage.md`, `output-templates.md`) as Project knowledge.
5. Open a chat in the Project and ask, for example: *"Screen AAPL, MSFT, NVDA, AMD, AVGO for revenue growth above 15%, positive free cash flow and price-to-sales below 15."* Claude will ask for your own SEC contact (`<ApplicationName> <email or URL>`) if none is configured.

Claude Code works too: open this repository (or copy the Skill folder to `.claude/skills/`); it has normal network access.

## Using the engine directly

```bash
python claude/skills/us-stock-research/scripts/research.py doctor --network --pretty
export SEC_USER_AGENT="MyResearchApp you@example.org"      # your own contact; never committed
python claude/skills/us-stock-research/scripts/research.py ingest --symbols AAPL NVDA TSM
python claude/skills/us-stock-research/scripts/research.py screen --spec claude/skills/us-stock-research/templates/screen-growth-drawdown.example.json
python claude/skills/us-stock-research/scripts/research.py evidence --symbol AAPL --pretty
```

Every command prints one JSON envelope (`status`, `data`, `warnings`, `errors`); see [`references/cli-reference.md`](claude/skills/us-stock-research/references/cli-reference.md). Data lives in a local DuckDB file (`DATABASE_PATH`; default `data/research.duckdb` in a checkout, `./research_data/` elsewhere). SEC access needs `SEC_USER_AGENT` (environment, `--sec-user-agent`, or a `.env` in the working directory; copy `.env.example`). Requests are rate limited and identify you to the SEC.

## Repository layout

```
claude/skills/us-stock-research/   the Skill: SKILL.md, references/, templates/, scripts/research.py
claude/project/                    Project instructions + setup guide
app/research/                      pure contracts: envelope, catalog, screen spec, screening, evidence, freshness
app/cli/research.py                the command line the Skill calls
app/services/                      use cases (only layer that opens repositories)
app/screening/                     metric rules = the financial methodology
app/ingestion, app/data, app/models, app/database   fetch, normalise, provenance, schema, DuckDB repositories
scripts/                           package_skill.py, run_p0.py, p1_gate.py, init_db.py, audit/
docs/                              data_coverage.yaml|md (coverage matrix, metric dictionary), architecture.md
tests/                             hermetic tests (simulated providers, temp databases)
```

Layering is enforced by tests: `cli -> services -> repositories -> DuckDB`; `app/research` is pure; SQL only in `app/database`.

## Methodology (summary; details in the Skill's references)

Net income = income attributable to common; growth, EPS growth, margin and leverage changes use one filing for both years; no TTM share counts; debt is never inferred zero; issuer-level market cap from the primary listing; price returns only (never "total return"), calendar-anchored, split-adjusted; Nasdaq and SEC SIC taxonomies kept separate (no GICS); TSM/IFRS fundamentals `UNSUPPORTED_TAXONOMY`; stale prices (older than 7 days before the as-of date) are blocked rather than shown as current; every value keeps its filing (accession, form, dates) or retrieval (provider, command, hash).

## Development

* Python **3.14** is the supported version (CI on Ubuntu and Windows); the engine also imports on 3.11+ but only 3.14 is claimed for CI.
* Install: `pip install --require-hashes -r requirements-dev.lock`
* Tests: `python -m pytest` (hermetic: no network, keys or models needed)
* Lint/format: `ruff check app tests scripts claude` and `ruff format --check app tests scripts claude`
* Package: `python scripts/package_skill.py --verify`
* Re-lock after changing a pin: `uv pip compile requirements.txt --universal --python-version 3.14 --generate-hashes -o requirements.lock` (same for `requirements-dev.txt`); CI fails on stale locks.
* P0 pilot (live, 14 securities): `python scripts/run_p0.py` (needs `SEC_USER_AGENT`); the manual `p0-ingestion` workflow runs it from GitHub. Simulated rehearsal: `python tests/p0_rehearsal.py` (mechanics only, never live evidence). Provider audit: `provider-probe` workflow.
* Rules for contributors and Claude Code: [`CLAUDE.md`](CLAUDE.md).

### Verification status
Provider behaviour was audited live on 2026-10-02 and the P0 pilot ran live on 14 securities (see `docs/data_coverage.md` section 13). The Claude-native layer (screening, evidence packets, CLI, packaging) is verified by hermetic tests on simulated providers; it has **not** been run live against SEC/Nasdaq/Cboe from the development environment, nor uploaded to claude.ai from it. Which Python version, packages and network access a given Claude environment provides is checked at run time by `research.py doctor`.

Not investment advice. The Skill gives no buy/sell recommendations by default.
