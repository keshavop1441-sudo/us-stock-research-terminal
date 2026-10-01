# US Stock Research Terminal

A local Windows application for researching US stocks. You will type requests such as
*"US tech stocks down 30-50% from their 52-week highs, revenue growth above 15%, positive free
cash flow, P/S below 5..."* and the terminal will screen the market and investigate the survivors
(filings, contracts, lawsuits, insider and institutional activity, earnings, news).

> **Status: foundation, hardened (Phase 1).** The app starts, the database exists with an idempotent
> write path, the pages are laid out, and health checks work. There is **no screening engine, no data
> loader and no LLM generation yet**, and the database is empty. Nothing here is fake data: anything
> missing shows as `N/A`.

## Supported Python and platforms

* **Python 3.14** is the supported version (`.python-version`; CI runs 3.14 on Ubuntu and Windows).
  The cloud sandbox this was written in only had Python 3.13, so the code is also kept importable on 3.13
  (e.g. named exception tuples instead of PEP 758 `except A, B:`); 3.13 is not a supported configuration.
* **Windows** is the target. See [Windows limitations](#windows-limitations) for what has and has not been run.

## Run on Windows

Requirements: Windows and [Python 3.14](https://www.python.org/downloads/) (include the `py` launcher).

* **`START_TERMINAL.bat`** - safe to run repeatedly. It creates `.venv` if missing, **rebuilds it if it is
  broken or not Python 3.14**, installs `requirements.lock` with hash checking (only when needed; a failed
  or interrupted install leaves no "installed" marker, so the next run retries), verifies the imports,
  creates/upgrades the database, then starts Streamlit on <http://127.0.0.1:8501> (this computer only) and
  opens the browser. Running it again while the terminal is up just opens the browser. Every failure stops
  the script with a message. Options: `/setup-only` (prepare, don't start), `/smoke` (start, check health, stop).
* **`UPDATE_DATA.bat`** - the future data-sync entry point. Today it verifies OpenBB V5 and exercises the
  single-writer lock; it downloads nothing. Exit codes: `0` ok, `1` failed, `2` another refresh/write is
  already running. `/nopause` skips the final key press (scheduled tasks).

Streamlit's first-run e-mail prompt is avoided by running it headless (the launcher opens the browser itself).

Manual equivalent:

```bat
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install --require-hashes -r requirements.lock
.venv\Scripts\python scripts\init_db.py
.venv\Scripts\python scripts\launch_terminal.py
```

Ollama is optional. If it is not running the app still starts and shows "Ollama unavailable".
To use it later: install Ollama and run `ollama pull qwen3.5:4b`.

## Architecture

```
ui page  ->  service  ->  repository  ->  DuckDB
(app/ui)    (app/services) (app/database)
```

* **UI pages** contain no SQL and never import the database, settings or OpenBB code; they call service
  functions and show what comes back (or why it is unavailable). Enforced by `tests/test_architecture.py`.
* **Services** (`app/services`) own use-cases (save a request, look up a company, collect status), translate
  low-level failures into `DataUnavailableError`, and are the only layer that opens repositories.
* **Repositories** (`app/database`) are the only place with SQL.

### Database ownership and concurrency (DuckDB, single writer)

DuckDB allows one read-write process per file, so the design is a simple explicit **single-writer** model:

| Who | Writes | How |
|---|---|---|
| The **refresh process** (`UPDATE_DATA.bat`) | bulk data tables, provenance | holds the writer lock for the whole refresh |
| The **Streamlit app** | user tables only (`query_history`, `research_runs`, `watchlists`, ...) | takes the writer lock for each (millisecond) action |

* **Lock:** an OS-level file lock `data/research.duckdb.writer.lock` (`filelock`: `fcntl` on POSIX, `msvcrt`
  on Windows). It works **across processes**; the OS releases it when the holder exits for any reason, so a
  crash or kill leaves no stale lock to clean up. The holder also writes `research.duckdb.writer.json`
  (operation, pid, start time) purely for display.
* **A second writer is refused** (`WriterBusyError`; `UPDATE_DATA.bat` exits with code 2 before doing any work).
* **Writes are transactional:** everything in one `writer(...)` block commits together or rolls back together,
  including when the process is killed mid-way.
* **Readers take no lock.** While a refresh has the file open, DuckDB cannot be opened by anyone else, so a
  read fails fast. The app then tells the user so: pages say "the database is being updated ... live data is
  unavailable", saving a request/watchlist is refused with the same explanation, and **Data Status shows the
  last reading taken by this app session marked `(cached)` with its timestamp, or `N/A` if there is none** -
  it is never shown as live. The cache lives in memory, so it is empty after an app restart.
* **Schema upgrades** (`ensure_database`) read first and only take the lock when something must change.
* **Timestamps are stored as UTC** (explicit defaults), independent of the machine's time zone.

### Read / write repository separation

* `ReadRepository` (`read_repository.py`): named read methods only, parameterised SQL, returns Polars frames.
  This is the only database interface that may ever be given to the future AI tool layer.
* `WriteRepository` (`write_repository.py`): named inserts and idempotent upserts taking validated record
  models (`app/models/records.py`); reachable only through `access.writer(...)`.
* Neither exposes `execute_sql` or a public connection. A DuckDB `read_only` connection cannot coexist with
  the app's read-write connections in one process, so it is deliberately **not** used as a security
  boundary; the boundary is the interface (tested), plus the tool registry below.
* DuckDB results are converted to Polars without pyarrow/pandas/numpy (`app/database/frames.py`).

### Identity, CIK normalization and idempotent ingestion

* **`normalize_cik`** (`app/models/identifiers.py`) is the single canonical function: `320193`, `"320193"`,
  `" 0000320193 "`, `"CIK320193"` -> `"0000320193"`. Invalid values (non-numeric, signed, floats, bools, zero,
  more than 10 digits) raise `InvalidCikError`. Record models, lookups and services all use it; the database
  additionally CHECKs the 10-digit form. Tickers go through `normalize_ticker` (upper-case, validated).
* **Business keys** (what "the same record" means; documented in `app/database/schema.py`):

| Table | Identity | Notes |
|---|---|---|
| `securities` | `(ticker, cik)` | cik may be unknown and is filled in later; enforced by the repository (DuckDB cannot update an indexed column of a foreign-key-referenced row) |
| `financial_facts` | `(cik, taxonomy, concept, unit, period_start, period_end, accession_no)` | one value as reported in one filing; later restatements are separate rows |
| `filings` | `accession_no` | |
| `price_daily` | `(security_id, trade_date)` | |
| `earnings` | `(cik, fiscal_year, fiscal_period)` | the report date is an attribute (announced dates move) |
| `ownership` | `(cik, holder_type, holder_key, as_of_date, accession_no, line_no)` | holder_key = holder CIK, else normalised name |
| `events` | `(cik, event_type, source_ref)` | source_ref = URL / accession+item / provider id |
| `sources` | append-only | one row per retrieval (provenance) |

  Keys with nullable parts are stored in a `*_key` primary-key column tied to its components by a CHECK
  constraint, so NULLs cannot defeat de-duplication. Reloading uses `INSERT ... ON CONFLICT DO UPDATE`
  (in-batch duplicates: last wins); tables are never deleted and reloaded.
* **Not solved yet (Phase 2 concern):** a company that *changes ticker* appears as a new `securities` row
  until a symbol-history source is added; share-class ticker spellings (`BRK.B`/`BRK-B`/`BRK/B`) are not
  reconciled; the same real-world event reported by two sources has two `source_ref`s.

### Tool registry (future AI tools)

`app/tools/registry.py`: every tool must declare name, description, input model, output model and
READ/WRITE access. The registry rejects WRITE tools (unless explicitly enabled), names/fields that suggest
SQL, shell, code execution, files or destructive operations, input models that allow unknown fields, and
unbounded free-text inputs; outputs are validated against the declared model. It validates declarations, not
handler bodies, so review of every future tool stays mandatory. No tools exist yet.

## Dependencies and reproducibility

* `requirements.txt` / `requirements-dev.txt`: **direct** dependencies, exactly pinned.
* `requirements.lock` / `requirements-dev.lock`: the full resolution with **hashes**, universal across
  Windows/Linux/macOS for Python 3.14 (platform-specific packages carry `sys_platform` markers).
  Install with `pip install --require-hashes -r requirements.lock`.
* To change a dependency edit the pin, then run
  `uv pip compile requirements.txt --universal --python-version 3.14 --generate-hashes -o requirements.lock`
  (and the same for `requirements-dev.txt`). CI recompiles and fails if the lock files differ.
* Tests verify that every pin is in the lock, every locked package has hashes, and that no third-party module
  is imported without being a declared direct dependency (this is how an undeclared `pyarrow` would be caught).
* OpenBB V5 = `openbb-core` 2.x with the `sec`, `nasdaq`, `cboe`, `news` extensions only. `openbb-core` and
  `openbb-sec` pull in fastapi, uvicorn, fastmcp and `openbb-platform-api`; this project does not use them.

## Configuration

Copy `.env.example` to `.env` (git-ignored). No API keys are needed for the current OpenBB providers.

| Variable | Default | Meaning |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama server |
| `OLLAMA_MODEL` | `qwen3.5:4b` | Model to use |
| `FAST_THINK` | `false` | Fast reasoning mode (wins if both flags are true) |
| `DEEP_THINK` | `true` | Deep reasoning mode |
| `DATABASE_PATH` | `data/research.duckdb` | Relative paths resolve against the project root |

## Tests and CI

```bat
.venv\Scripts\python -m pip install --require-hashes -r requirements-dev.lock
.venv\Scripts\python -m pytest
```

Tests cover configuration; identifiers; DuckDB->Polars conversion (including a subprocess where
pyarrow/pandas/numpy cannot be imported); schema, constraints and the v1->v2 migration; idempotent upserts
for every table; read/write repository separation; cross-process locking (a real second process, including
being killed); stale/unavailable behaviour while a refresh runs; every page via Streamlit `AppTest`; layering
rules; tool-registry validation; the bootstrapper; the launcher (including a real start/health/stop cycle);
dependency declarations and lock files; OpenBB V5 imports; Ollama detection.

GitHub Actions (`.github/workflows/ci.yml`): tests + lint on **Ubuntu and Windows, Python 3.14**; lock
freshness; a minimal environment with only duckdb and polars; and a **real Windows run of the `.bat` files**.

### Windows limitations

What has actually been executed, and where:

* **GitHub Actions `windows-latest`, Python 3.14** runs the full test suite and the real `.bat` files
  (`START_TERMINAL.bat /setup-only` twice, broken-install recovery, `/smoke`, `UPDATE_DATA.bat /nopause`).
  Its first run found three Windows-only problems that Linux could not show (DuckDB's "file in use" error has
  different wording on Windows and was not recognised; tests assumed UTF-8 source reading and an instantly
  refused closed port). They were fixed and the second run passed. The CI result is the source of truth.
* **No developer-PC run:** nobody has run the scripts on a personal Windows installation (different Python
  install layout, antivirus, corporate proxy, OneDrive-synced folders, non-ASCII user names). Treat the first
  such run as untested.
* Antivirus/backup tools that open `data/research.duckdb` make the database look "in use"; the app reports
  that as locked/unavailable instead of crashing. Keep the project folder out of OneDrive-style sync.
* On Windows a closed local port can take ~2 s to time out instead of refusing instantly, so the Data Status
  and Settings pages can take that long to load while Ollama is not running.
* GitHub warns that `actions/checkout@v4` and `actions/setup-python@v5` use the deprecated Node 20 runtime;
  CI still runs (bump the action versions later).

## Project layout

```
app/
  main.py          Streamlit entry: settings, DB check, st.navigation
  config.py        Settings (environment / .env)
  ui/              One script per page + helpers (no SQL, no database imports)
  services/        Use-cases for the UI; the only layer that opens repositories
  database/        schema, migrations, locking, connection, access, read_repository, write_repository, frames
  models/          identifiers (CIK/ticker), records (write inputs), status models
  agent/           LLMProvider abstraction, OllamaProvider (availability detection only)
  tools/           Tool registry (no tools yet)
  data/            OpenBB V5 access (lazy import)
  screening/ research/   (planned)
scripts/           bootstrap_env.py, launch_terminal.py, init_db.py, update_data.py, setup_env.bat
tests/             pytest suite + hold_database.py / minimal_frames_check.py helpers
data/              research.duckdb lives here (git-ignored)
```

## Remaining work

1. Data-source coverage audit, then loaders (via OpenBB): securities + CIK map, SEC company facts, prices,
   filings, earnings, ownership, news/events.
2. Screening engine; natural-language -> structured screen (LLM generation, first read-only tools).
3. Company research page and research-run workflow.
4. Watchlist items, saved screens, charts.
