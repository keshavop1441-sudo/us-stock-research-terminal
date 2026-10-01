# US Stock Research Terminal

A local Windows application for researching US stocks. You will type requests such as
*"US tech stocks down 30-50% from their 52-week highs, revenue growth above 15%, positive free
cash flow, P/S below 5..."* and the terminal will screen the market and investigate the survivors
(filings, contracts, lawsuits, insider and institutional activity, earnings, news).

> **Status: foundation only.** The app starts, the database exists, the pages are laid out, and
> health checks work. There is **no screening engine, no data loader and no LLM generation yet**,
> and the database is empty. Nothing here is fake data: anything missing shows as `N/A`.

## What works today

| Area | State |
|---|---|
| Streamlit multipage app (`st.Page` / `st.navigation`) | Home, Screener, Company, Watchlists, Data Status, Settings |
| Home | Natural-language box; requests are saved to `query_history`; says plainly that interpretation is not built yet |
| Screener / Company | Page structure only (Company can look up a ticker and chart stored prices once data exists) |
| Watchlists | Create and list watchlists |
| Data Status | Database path/status, row counts, last sync, OpenBB package status (+ on-demand runtime check), Ollama status |
| Settings | Shows Ollama URL/model, thinking flags, provider status (read-only; edit `.env`) |
| DuckDB schema | 12 tables (+ `schema_meta`), created automatically at `data/research.duckdb` |
| LLM | `LLMProvider` abstraction + `OllamaProvider` (availability detection only; generation is a placeholder) |

## Run on Windows

Requirements: Windows, [Python 3.14](https://www.python.org/downloads/) with the `py` launcher.

1. Double-click **`START_TERMINAL.bat`**. On first run it creates `.venv`, installs
   `requirements.txt`, copies `.env.example` to `.env`, creates the database, and starts Streamlit.
2. Open http://127.0.0.1:8501 if the browser does not open by itself. The server listens on
   localhost only.
3. **`UPDATE_DATA.bat`** is the future data-sync entry point. Today it initialises the database and
   verifies that OpenBB V5 loads; it downloads nothing yet.

Manual equivalent:

```bat
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python scripts\init_db.py
.venv\Scripts\python -m streamlit run app\main.py
```

Ollama is optional. If it is not running, the app still starts and shows "Ollama unavailable".
To use it later: install Ollama and run `ollama pull qwen3.5:4b`.

## Configuration

Copy `.env.example` to `.env` (git-ignored). No API keys are needed for the current OpenBB providers.

| Variable | Default | Meaning |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Local Ollama server |
| `OLLAMA_MODEL` | `qwen3.5:4b` | Model to use |
| `FAST_THINK` | `false` | Fast reasoning mode (wins if both flags are true) |
| `DEEP_THINK` | `true` | Deep reasoning mode |
| `DATABASE_PATH` | `data/research.duckdb` | Relative paths resolve against the project root |

## Tests

```bat
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

Covered: configuration, database creation/constraints/repository, application startup and every
page (Streamlit `AppTest`), OpenBB V5 imports and API surface, Ollama detection (mocked transport
plus a real closed port), the tool registry, and status collection. Tests use temporary databases
and never need Ollama, API keys or network access.

## Project layout

```
app/
  main.py          Streamlit entry: settings, DB init, st.navigation
  config.py        Settings (environment / .env)
  ui/              One script per page (home, screener, company, watchlists, data_status, settings) + helpers
  agent/           LLMProvider abstraction, OllamaProvider
  tools/           Registry of the only operations an LLM may call (empty for now)
  screening/       (planned) criteria + calculated metrics
  research/        (planned) investigation workflows
  data/            OpenBB V5 access
  database/        DuckDB schema, connections, repository
  models/          Pydantic status models
  services/        Status collection for the UI
scripts/           init_db.py, update_data.py
tests/
data/              research.duckdb lives here (git-ignored)
```

## Data model

`securities`, `financial_facts`, `price_daily`, `filings`, `earnings`, `ownership`, `events`,
`sources`, `research_runs`, `query_history`, `watchlists`, `watchlist_items`
(plus `schema_meta` for the schema version).

* **Raw vs. calculated:** tables hold only raw reported data. Ratios (P/S, growth, margins,
  % off 52-week high...) are computed at query time and never get their own tables.
* **Identity:** `cik` (10-digit text, e.g. `0000320193`) identifies the issuer and keys issuer-level
  data. A ticker is just a label on a `securities` row (surrogate `security_id`); one CIK can have
  several securities (GOOG/GOOGL) and tickers can change or be reused.
* **Provenance:** `sources` records each retrieval; `max(retrieved_at)` is the "last synchronization".

## OpenBB V5

Uses OpenBB V5 only: `openbb-core` 2.x with the `sec`, `nasdaq`, `cboe` and `news` extensions
(not the full `openbb` meta-package). V5 is organised by provider, e.g. `obb.sec.income_statement(...)`,
`obb.nasdaq.equity.historical(...)`, `obb.news.company(...)`. V4 examples (`obb.equity.price.historical`)
do not apply. Importing OpenBB takes several seconds, so the UI imports it only on request.

## Security

The future LLM never receives arbitrary SQL, shell, Python execution or database write access. It can
only request tools registered in `app/tools/registry.py`, with pydantic-validated arguments. The
server binds to `127.0.0.1`. Secrets live in `.env`, which is git-ignored.

## Remaining work

1. Data loaders (via OpenBB): securities + CIK map, SEC company facts, daily prices, filings, earnings, ownership, news/events.
2. Screening engine: structured criteria, calculated metrics, results.
3. Natural-language query → structured screen (LLM generation in `OllamaProvider`, tool registry entries).
4. Company research page and research-run workflow (filings, contracts, lawsuits, insiders, news).
5. Watchlist items, saved screens, charts.
