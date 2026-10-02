# CLAUDE.md

Claude-native US stock research. The repository is the source-controlled implementation and methodology of a **Claude Skill** (`claude/skills/us-stock-research`) used from a Claude Project. The user's interface is Claude; Python is the deterministic engine behind it. See README.md for the product, `docs/architecture.md` for the design and migration record.

## Product principle (never violate)
**The LLM is never the source of truth for numbers.** Python owns: financial facts, derived metrics, growth, margins, leverage, share counts, drawdowns, valuation, date and filing-period alignment, missing-data semantics, screening. Claude understands the request, runs the workflow, explains validated JSON, synthesises qualitative evidence and states what is unknown. Never write code or prompts that let a model produce, round, estimate or "fill in" a financial value. Missing/unavailable/unsupported/unreported/unreliable is NEVER zero.

## Stack (do not add others without a concrete requirement)
Python 3.14, OpenBB V5 (providers only), DuckDB, Polars, Pydantic, python-dotenv, httpx, filelock. Pandas only where genuinely useful.
No local LLM (Ollama/Qwen/DeepSeek...), no Streamlit/Plotly/React/Node/Docker/PostgreSQL/Redis/FastAPI/vector DBs, no hosted web app, no paid data APIs (FMP etc.).
(`openbb-core` pulls in fastapi/uvicorn/pandas/numpy transitively; the project does not use them.)

## Repository map
- `app/research/` pure contracts (no I/O): `envelope` (JSON result), `catalog` (screenable metrics), `screen_spec` (strict spec), `screening` (evaluation), `evidence` (packet + SEC filing classification), `freshness`, `format`, `universe`.
- `app/services/` use cases; the only layer that opens repositories. `research_*_service.py` serve the Skill; `ingestion_service.py` is the audited P0 pipeline; `p0_metrics_service.py` builds snapshots.
- `app/cli/research.py` the single command line (`doctor catalog validate-screen resolve ingest universe metrics screen evidence events`); prints ONE JSON envelope.
- `app/screening/` metric rules (`metrics.py`, `fundamentals.py`, `snapshot.py`, `classification.py`) - the methodology in code.
- `app/ingestion/`, `app/data/`, `app/models/`, `app/database/` fetch, normalise, provenance, schema, repositories.
- `claude/skills/us-stock-research/` the Skill (SKILL.md, references/, templates/, scripts/research.py launcher). `claude/project/` Project instructions + setup guide.
- `scripts/package_skill.py` builds `dist/us-stock-research.zip`; `scripts/run_p0.py`, `scripts/p1_gate.py` P0 pilot and gate; `scripts/init_db.py`; `scripts/audit/` provider probe.
- `docs/data_coverage.yaml|md` coverage matrix and metric dictionary. `tests/` hermetic tests.

## Commands
- Tests: `python -m pytest` (needs `requirements-dev.lock`; no network or keys required)
- Lint/format: `ruff check app tests scripts claude` and `ruff format app tests scripts claude` (line length 120, target py314)
- Engine: `python claude/skills/us-stock-research/scripts/research.py <command> --pretty` (e.g. `doctor`, `catalog`)
- Package the Skill: `python scripts/package_skill.py` (then `--verify`); output `dist/` is git-ignored
- Init/upgrade DB: `python scripts/init_db.py`
- Re-lock after changing a pin in requirements*.txt:
  `uv pip compile requirements.txt --universal --python-version 3.14 --generate-hashes -o requirements.lock`
  (same for requirements-dev.txt -> requirements-dev.lock). CI fails if the lock files are stale.

## Python version
Python 3.14 is the supported target (`.python-version`, CI). Cloud sandboxes may only have 3.13 (the engine also imports on 3.11+), so do NOT write PEP 758 `except A, B:` (ruff format produces it from a parenthesised tuple - use a named tuple constant, see `app/database/locking.py`) and keep quoted self-references (UP037 ignored on purpose). Never claim something ran on 3.14 or Windows unless it did (CI does that).

## Layering (enforced by tests/test_architecture.py)
`cli -> services -> repositories -> DuckDB`; `app/research` is pure (imports no database, provider, HTTP or environment code). The CLI never opens a repository. SQL lives only in `app/database`. Only services open repositories. Research read services never import the write side.

## What is safe to change / what is not
- Safe: Skill docs/templates, output wording, CLI plumbing, new read-only evidence sections, new catalog entries that are backed by an existing metric function.
- Needs a test AND a YAML entry: any new derived metric (define it in `app/screening/metrics.py`/`snapshot.py`, add it to `app/research/catalog.py`, `docs/data_coverage.yaml`, and tests of every non-OK state).
- Never: relax a methodology rule below, convert a non-OK state to a number, add a source without an audit entry, hard-code an SEC contact, add execution surfaces (arbitrary SQL, shell, Python eval) to the CLI.

## Database (data/research.duckdb or $DATABASE_PATH)
- Schema: `app/database/schema.py` (documents every table's business key). Bump `SCHEMA_VERSION`, add a function to `MIGRATIONS` in `migrations.py`, and test the upgrade whenever the schema changes. `query_history`, `research_runs`, `watchlists*` are LEGACY tables of the retired terminal (unused).
- Raw facts only; never add per-ratio tables (compute ratios at query time in `app/screening`).
- Identity: `cik` (10-digit text) via `normalize_cik` - the ONLY place CIKs are normalised. Tickers via `normalize_ticker`/`canonical_symbol`. Issuer-level tables key on cik, listed-security tables on `security_id`.
- Idempotent ingestion: `WriteRepository.upsert_*` with validated records from `app/models/records.py`; never delete-and-reload. New ingested table => business key in the schema docstring, a `*_key`/composite PK, an `upsert_*` and a "same record twice" test.
- Missing data is NULL (shown `N/A`). Never fabricate production data; fixtures belong in tests only (simulated runs never count as live evidence).
- Timestamps are naive UTC. DuckDB: UPDATEs of indexed columns of FK-referenced rows fail, so mutable identity is enforced in the repository.
- DuckDB -> Polars via `app/database/frames.to_polars` (no `.pl()`/`.df()`). SQL is fixed text with bound parameters, never built from user or LLM text.

## Concurrency (single writer)
Reads: `access.reader` (no lock; fails fast with `DatabaseLockedError`). Writes: `access.writer(path, operation)` = cross-process writer lock + one transaction. `ingest` holds the lock for its whole run. Outputs must show unavailable/stale honestly (`app/research/freshness.py`: price/quote metrics older than 7 days before the as-of date become MISSING_INPUT unless `--allow-stale`).

## Methodology (do not relax - Phase 2/3A)
- Net income = `net_income_to_common` (NetIncomeLoss). Growth uses values from the SAME filing (`same_filing_pair`); EPS growth is split-safe; no TTM share counts; margin and leverage changes use one filing, else NOT_COMPARABLE.
- Debt is never inferred zero: `total_debt` is 0 only if all core lines are reported or the filing explicitly evidences no debt; otherwise MISSING_INPUT. No filer-type shortcut.
- Market cap: `issuer_market_cap` (one per CIK, primary listing) is the only market cap for issuer valuation; `security_market_cap` is UNAVAILABLE. Undesignated multi-class issuers use the first loaded listing and say so (`primary_listing_designation`).
- Returns are PRICE returns (never "total return"); closes are split-adjusted; anchored on calendar dates; 52-week drawdown vs the 52-week high.
- Classification: Nasdaq sector/industry and SEC SIC are separate taxonomies; GICS unused; a screen filters in ONE named taxonomy; raw values are stored with their source.
- Derived metrics return a `MetricResult` (OK / MISSING_INPUT / ZERO_DENOMINATOR / NOT_MEANINGFUL / NOT_COMPARABLE) and live in `app/screening/metrics.py`.
- TSM / IFRS: `EXPECTED_UNSUPPORTED` for financial facts (`UNSUPPORTED_TAXONOMY:ifrs-full`); identity, prices, quotes still work; fundamentals stay MISSING_INPUT; never fake US-GAAP facts.
- SEC `BRK-B` vs Nasdaq/Cboe `BRK.B`: `app/models/symbols.py`; identity is the CIK. Dates/periods: `app/models/periods.py`.
- Screens: Claude translates to a `ScreenSpec`, Python filters. No black-box score, no ranking; output lists matches, failures and missing-data exclusions with exact values, thresholds, per-criterion results and provenance. The full market universe is NOT loaded (`universe_pilot` in the coverage YAML): screens run on ingested securities.

## Data sources, access and rate limits
SEC (`company_tickers_exchange`, `submissions`, `companyfacts`), Cboe and Nasdaq through OpenBB V5 (`obb.cboe.*`, `obb.nasdaq.*`; never copy V4 examples; check `obb.coverage.commands`). Import OpenBB lazily (`app/data/openbb_client.get_obb`). `SEC_USER_AGENT` (`<ApplicationName> <contact email or URL>`) is required for SEC access; it comes from the environment, `--sec-user-agent` or a `.env`; never hard-code or invent one. All SEC calls go through `SecHttpClient` (rate ceiling ~8/s, 403 never retried). Web research by Claude is allowed ONLY for qualitative events and must cite publisher, URL and date; it never replaces engine numbers.
- `docs/data_coverage.yaml` is the coverage matrix and metric dictionary; after editing run `python tests/render_coverage.py`; `tests/test_data_coverage.py` validates it. Never mark LIVE_VERIFIED without a real live call (the dev sandbox cannot reach providers; use the `provider-probe` workflow and cite the run id).
- Phase 3A P0 pilot (14 securities): `scripts/run_p0.py` (live), `tests/p0_rehearsal.py` (SIMULATED). The manifest is pinned (`app/ingestion/manifest.py`); fetch / normalise / calculate stay separate; derived metrics are never stored.

## Skill artifacts
- Runtime packages are NOT declared in frontmatter (there is no `dependencies` key; `package_skill.py` rejects unknown keys). A fresh Skill environment installs them via the SKILL.md setup steps: `python -m pip install --require-hashes -r scripts/requirements.lock`. The launcher `scripts/research.py` (stdlib only) turns a missing package into a `DEPENDENCIES_MISSING` JSON envelope with that command; it never installs anything itself.
- `claude/skills/us-stock-research/SKILL.md` has only `name` (= folder name, lowercase-hyphen, no "claude"/"anthropic") and `description` (<= 1024 chars, says when to use and not use) in its frontmatter. Keep the body short; detail lives in `references/` (progressive disclosure). The four Project knowledge files are `references/{research-methodology,metric-definitions,sources-coverage,output-templates}.md`.
- Package with `scripts/package_skill.py`: one top-level folder `us-stock-research/`, bundled engine under `scripts/_engine/app`, fixed timestamps, LF endings, refuses secrets/local paths/forbidden files. Rebuild after any change to `app/`, `requirements*.txt` or the Skill.
- Project instructions for the Claude Project live in `claude/project/project-instructions.md`; setup steps in `claude/project/SETUP.md`. Never put keys or a personal SEC contact in any of them.

## How to validate a change
`ruff check app tests scripts claude && ruff format --check app tests scripts claude && python -m pytest && python scripts/package_skill.py --verify`. Add tests for every new state/branch; hermetic only (temp databases, `tests/p0_fakes.py` simulated SEC/OpenBB). Do not weaken existing tests.
