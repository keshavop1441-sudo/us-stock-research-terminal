# CLAUDE.md

Local Windows stock-research terminal. Foundation milestone only - see README.md for status and roadmap.

## Stack (do not add others without a concrete requirement)
Python 3.14, Streamlit, OpenBB V5, DuckDB, Polars, Plotly, Pydantic, python-dotenv, httpx.
Pandas only where genuinely useful. No React/Node/Docker/PostgreSQL/MongoDB/Redis/FastAPI/Kubernetes/vector DBs/packaging.
(`openbb-core` pulls in fastapi/uvicorn transitively; the project does not use them.)

## Commands
- Tests: `python -m pytest` (needs `requirements-dev.lock`; no network, Ollama or keys required)
- Lint/format: `ruff check app tests scripts` and `ruff format app tests scripts` (line length 120, target py314)
- Run: `python scripts/launch_terminal.py` (or `streamlit run app/main.py`)
- Init/upgrade DB: `python scripts/init_db.py`
- Re-lock after changing a pin in requirements*.txt:
  `uv pip compile requirements.txt --universal --python-version 3.14 --generate-hashes -o requirements.lock`
  (same for requirements-dev.txt -> requirements-dev.lock). CI fails if the lock files are stale.

## Python version
Python 3.14 is the supported target (`.python-version`, CI). Cloud sandboxes may only have 3.13, so keep code
importable on both: do NOT write PEP 758 `except A, B:` (ruff format would produce it from a parenthesised
tuple - use a named tuple constant, see `app/database/locking.py`) and keep quoted self-references (UP037 is
ignored on purpose). Never claim something ran on 3.14 or Windows unless it did (CI does that).

## Layering (enforced by tests/test_architecture.py)
`ui page -> service -> repository -> DuckDB`. UI pages import only `app.services` (+ streamlit/polars/plotly):
no SQL, no `app.database`, no settings/env, no OpenBB. SQL lives only in `app/database`. Only services open
repositories. Agent/tool code may never import the write side or a connection.

## Database (data/research.duckdb)
- Schema: `app/database/schema.py` (documents every table's business key). Bump `SCHEMA_VERSION`, add a
  function to `MIGRATIONS` in `migrations.py`, and test the upgrade whenever the schema changes.
- Raw facts only; never add per-ratio tables (compute ratios at query time in `app/screening`).
- Identity: `cik` (10-digit text) via `normalize_cik` - the ONLY place CIKs are normalised; never pad/strip
  CIKs elsewhere. Tickers via `normalize_ticker`. Issuer-level tables key on cik, listed-security tables on
  `security_id`.
- Idempotent ingestion: write through `WriteRepository.upsert_*` with validated records from
  `app/models/records.py`; never delete-and-reload a table. New ingested table => define its business key in
  the schema docstring, a `*_key`/composite PK, an `upsert_*` method and a "same record twice" test.
- Missing data is NULL (shown as `N/A`). Never fabricate production data; fixtures belong in tests only.
- Timestamps are naive UTC (`NOW_UTC` defaults). DuckDB: UPDATEs of indexed columns of FK-referenced rows
  fail, so mutable identity (ticker, watchlist name) is enforced in the repository.
- DuckDB -> Polars via `app/database/frames.to_polars` (no `.pl()`/`.df()`: they need undeclared packages).
- SQL is fixed text with bound parameters. Never build SQL from user or LLM text.

## Concurrency (single writer)
Reads: `access.reader` (no lock; fails fast with `DatabaseLockedError` if a refresh owns the file).
Writes: `access.writer(path, operation)` = cross-process writer lock + one transaction. Services use
`services/db.py` (`read_access` / `write_access`) which turns failures into `DataUnavailableError`.
The refresh process holds the lock for its whole run. UI must show "unavailable/cached" honestly, never stale
data as live (`status_service`). A `read_only` DuckDB flag is NOT usable as a boundary in-process.

## Security rules for the LLM layer
The LLM must never get: arbitrary SQL, shell access, arbitrary Python execution, or direct database write access.
It may only call tools registered in `app/tools/registry.py`: declared name/description/input+output models/access;
handlers may call only `ReadRepository` methods. WRITE tools are disabled. Never commit API keys; configuration
comes from `.env` (git-ignored). Keep the server bound to 127.0.0.1.

## Dependencies
Pin direct deps exactly in `requirements*.txt`; install only from the hash-locked `*.lock` files. Every
third-party import must be a declared direct dependency (tested). Do not rely on transitive packages
(that is how an undeclared pyarrow slipped in).

## Windows scripts
`START_TERMINAL.bat` / `UPDATE_DATA.bat` call `scripts/setup_env.bat` -> `scripts/bootstrap_env.py` (hash-locked,
idempotent, marker only after a verified install). Keep them CRLF + ASCII, check `errorlevel` after every step,
and keep logic in Python where it can be tested. Real Windows verification = the `windows-scripts` CI job.

## OpenBB - V5 only
- V5 = `openbb-core` 2.x. Installed: core + `openbb-sec`, `-nasdaq`, `-cboe`, `-news`. Do not install the full ecosystem.
- API is provider-namespaced: `obb.sec.*`, `obb.nasdaq.equity.*`, `obb.cboe.*`, `obb.news.company`.
  Check `obb.coverage.commands` / `inspect.signature` before using a command. **Never copy V4 examples**
  (`obb.equity.price.historical`, `provider="yfinance"/"fmp"` ...).
- `import openbb` is slow: import lazily (`app/data/openbb_client.get_obb`). Never import it at page load.

## Streamlit conventions
- `st.Page` + `st.navigation` in `app/main.py`; each page is a script in `app/ui/` (file-based so `AppTest.switch_page` works).
- `st.set_page_config` only in `main.py`. Use `width="stretch"` (not the deprecated `use_container_width`).
- The app must start and every page must render with no Ollama, no network and an empty database.
- Page and service code catches infrastructure errors and shows them; it does not crash.

## Testing
Hermetic: temp databases (`tmp_path`), Ollama pointed at a closed port or a mock transport. Use `AppTest` for UI behaviour.

## Data sources and metric rules (Phase 2 audit)
- `docs/data_coverage.yaml` is the machine-readable coverage matrix and metric dictionary (statuses LIVE_VERIFIED /
  API_SHAPE_VERIFIED / FIXTURE_VERIFIED / NOT_VERIFIED / UNAVAILABLE / DERIVED); `docs/data_coverage.md` is the summary.
  After editing the YAML run `python tests/render_coverage.py`; `tests/test_data_coverage.py` validates it.
- Never mark something LIVE_VERIFIED without a real live call (the dev sandbox cannot reach providers: use the
  `provider-probe` workflow and cite the run id). Do not pass fixtures or unit tests off as live evidence.
- Derived metrics live in `app/screening/metrics.py` and return a `MetricResult` (OK / MISSING_INPUT / ZERO_DENOMINATOR /
  NOT_MEANINGFUL / NOT_COMPARABLE); never turn a non-OK state into 0 or a percentage. Growth uses values from the same
  filing (`same_filing_pair`); net income means `net_income_to_common`; market cap is per issuer; no TTM share counts.
- SEC `BRK-B` vs Nasdaq/Cboe `BRK.B`: use `app/models/symbols.py`; identity is the CIK. Dates/periods: `app/models/periods.py`.

