# CLAUDE.md

Local Windows stock-research terminal. Foundation milestone only - see README.md for status and roadmap.

## Stack (do not add others without a concrete requirement)
Python 3.14, Streamlit, OpenBB V5, DuckDB, Polars, Plotly, Pydantic, python-dotenv, httpx.
Pandas only where genuinely useful. No React/Node/Docker/PostgreSQL/MongoDB/Redis/FastAPI/Kubernetes/vector DBs/packaging.
(`openbb-core` pulls in fastapi/uvicorn transitively; the project does not use them.)

## Commands
- Tests: `python -m pytest` (needs `requirements-dev.txt`; no network, Ollama or keys required)
- Lint/format: `ruff check app tests scripts` and `ruff format app tests scripts` (line length 120)
- Run: `streamlit run app/main.py`
- Init DB: `python scripts/init_db.py`

## OpenBB - V5 only
- V5 = `openbb-core` 2.x. Installed: core + `openbb-sec`, `-nasdaq`, `-cboe`, `-news`. Do not install the full ecosystem.
- API is provider-namespaced: `obb.sec.*`, `obb.nasdaq.equity.*`, `obb.cboe.*`, `obb.news.company`.
  Check `obb.coverage.commands` / `inspect.signature` before using a command. **Never copy V4 examples**
  (`obb.equity.price.historical`, `provider="yfinance"/"fmp"` ...).
- `import openbb` is slow: import lazily (`app/data/openbb_client.get_obb`). Never import it at page load.

## Database (data/research.duckdb)
- Schema lives in `app/database/schema.py`; bump `SCHEMA_VERSION` and add a migration when it changes.
- Raw facts only. Never add per-ratio tables; compute ratios at query time (`app/screening`).
- `cik` (10-digit text) is the issuer identity; ticker is not an identity. Issuer-level tables key on cik,
  listed-security tables on `security_id`.
- Missing data is NULL (shown as `N/A`). Never fabricate production data; fixtures belong in tests only.
- DuckDB: one connection configuration per process (use `database.connection.connect`, short-lived, read-write).
  Do not put UNIQUE/indexed mutable columns on rows referenced by foreign keys (updates fail) - enforce in the repository.
- SQL is fixed text with bound parameters. Never build SQL from user or LLM text.

## Security rules for the LLM layer
The LLM must never get: arbitrary SQL, shell access, arbitrary Python execution, or direct database write access.
It may only call tools registered in `app/tools/registry.py` (validated pydantic args, each wrapping one reviewed operation).
Never commit API keys; configuration comes from `.env` (git-ignored). Keep the server bound to 127.0.0.1.

## Streamlit conventions
- `st.Page` + `st.navigation` in `app/main.py`; each page is a script in `app/ui/` (file-based so `AppTest.switch_page` works).
- `st.set_page_config` only in `main.py`. Use `width="stretch"` (not the deprecated `use_container_width`).
- The app must start and every page must render with no Ollama, no network and an empty database.
- Page and service code catches infrastructure errors and shows them; it does not crash.

## Testing
Hermetic: temp databases (`tmp_path`), Ollama pointed at a closed port or a mock transport. Use `AppTest` for UI behaviour.
