# Architecture and migration record

## Product
A Claude Project plus the **us-stock-research** Skill. The user types a natural-language request in the Project; Claude invokes the Skill; the Skill runs deterministic Python over public data; Claude receives validated JSON evidence and writes the research answer. The repository is the source-controlled implementation and methodology. Nothing is hosted and no local LLM is used.

```
Claude Project (instructions + 4 reference files)
        |  natural language
        v
Claude  --invokes-->  Skill: SKILL.md + references/ + scripts/research.py (+ bundled engine)
                                   |  JSON envelope (stdout)
                                   v
              app/cli  ->  app/services  ->  app/database (DuckDB, raw facts + provenance)
                                |                 ^
                                |  app/research   |  ingestion: SEC HTTP (rate limited, User-Agent)
                                |  (pure contracts)|           OpenBB V5: Cboe prices, Nasdaq quotes
                                v                 |
                         app/screening (metric rules: the methodology)
```

Claude's role: translate requests into screen specs, choose workflows, explain validated results, synthesise qualitative evidence (attributed web research), state unknowns. Python's role: every number, date alignment, missing-data decision, screen.

## Operations (the CLI is the whole interface)
resolve securities (`resolve`), identity / prices / quotes / SEC facts (`ingest`, stages), canonical metrics incl. growth, margins, leverage, share count, drawdowns, valuation inputs (`metrics`), structured screen (`validate-screen`, `screen`), evidence packet (`evidence`), filing-event evidence (`events`), readiness (`doctor`), schema (`catalog`). All output is one JSON envelope (`app/research/envelope.py`).

## Migration audit (what happened to what)
| Area | Decision |
|---|---|
| `app/screening`, `app/ingestion`, `app/models`, `app/database`, `app/data`, DuckDB/Polars, provenance, P0 pilot, P1 gate, coverage matrix | **Retained** (the engine); small additions only |
| `app/services/ingestion_service.py`, `p0_metrics_service.py`, `db.py` | **Retained / generalised**: stages selectable, arbitrary symbols, all stored listings of an issuer load so the issuer-level market cap uses the primary listing |
| `app/ui/*`, `app/main.py`, `.streamlit/`, `START_TERMINAL.bat`, `UPDATE_DATA.bat`, `scripts/{setup_env.bat,bootstrap_env.py,launch_terminal.py,update_data.py}` | **Deleted** (Streamlit terminal and Windows launchers) |
| `app/agent/` (Ollama provider), `OLLAMA_*`, `FAST_THINK`, `DEEP_THINK` settings | **Deleted** (no local LLM) |
| `app/tools/registry.py` (tool registry for a local agent) | **Deleted**: the fixed CLI command set replaces it; no execution surface is exposed |
| `app/services/{company,watchlist,query,startup,status}_service.py` | **Deleted** (UI-only) |
| `streamlit`, `plotly` (and transitive `pyarrow`) | **Removed** from requirements and locks |
| Legacy tables `query_history`, `research_runs`, `watchlists*` | **Kept in the schema** (no migration risk), documented LEGACY, unused |
| Windows CI job for the `.bat` scripts | **Replaced** by a Windows `skill-package` job (build, extract, run the packaged Skill) plus the existing Windows test job |
| Tests of the UI, Ollama, launchers, batch files, bootstrap, tool registry | **Deleted with their code**; replaced by architecture, CLI, screening, evidence, packaging and no-retired-stack tests |
| New | `app/research/*`, `app/cli/research.py`, `app/services/research_*`, `claude/`, `scripts/package_skill.py`, new metrics (`*_margin_change_yoy`, `debt_to_equity_change_yoy`) |

## Deliberate limits
Screens run on ingested securities (the full-universe load is gated by the Phase 3 staged pilot). Insider transactions, 13F holders, news, government awards and litigation text are not parsed; their evidence-packet sections say `NOT_COLLECTED` with guidance for attributed web research. IFRS issuers (TSM) have no fundamentals.

## Where the Skill can run
Claude Code and claude.ai (code execution, with network access to `www.sec.gov`, `data.sec.gov`, `api.nasdaq.com`, `cdn.cboe.com`). The Claude API's code-execution container has no network and no package installation, so live ingestion cannot run there. Not verified from this repository: the exact claude.ai sandbox Python version and whether OpenBB installs in it; `research.py doctor --network` reports what a given environment can do.
