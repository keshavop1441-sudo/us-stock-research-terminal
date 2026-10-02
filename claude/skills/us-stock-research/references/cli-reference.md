# CLI reference: `python scripts/research.py`

Global options (before the command): `--db PATH`, `--sec-user-agent "<App> <contact>"`, `--pretty`.
Every command prints **one JSON envelope** to stdout and nothing else.
Diagnostics from libraries (for example OpenBB's first-run "Extensions to add" / "Building..." messages) go to stderr, never to stdout.

```json
{"schema_version": "1.0", "command": "screen", "status": "OK|PARTIAL|ERROR",
 "generated_at": "2026-01-01T00:00:00", "data": {}, "warnings": [{"code": "...", "message": "..."}], "errors": []}
```

* `OK` everything requested was produced. `PARTIAL` something is missing (read `warnings` and per-item states). `ERROR` nothing usable (read `errors`).
* Exit codes: `0` data returned (OK/PARTIAL), `1` ERROR, `2` invalid arguments/specification.
* Every metric is `{"state": "OK|MISSING_INPUT|ZERO_DENOMINATOR|NOT_MEANINGFUL|NOT_COMPARABLE", "value": number|null, "reason": code|null, "flags": []}`. `value` is null unless `state` is `OK`.
* Fractions are fractions: `0.15` = 15%.

## Commands

| Command | Arguments | Notes |
|---|---|---|
| `doctor` | `--network` `--deep` | Readiness: Python, packages, OpenBB, SEC_USER_AGENT (value never shown), database, host reachability. `capabilities` says which workflows can run. |
| `catalog` | | Screenable metrics (scope, unit, description) and the JSON Schema of a screen spec. |
| `validate-screen` | `--spec FILE` or `--spec-json JSON` | Validates strictly; returns normalised spec and thresholds in stored units. Exit 2 with `errors[].details.errors` listing each field problem. |
| `resolve` | `--symbols A B` | Live: SEC ticker map. `data.resolved` / `data.unresolved`. Nothing guessed. |
| `ingest` | `--symbols A B` `--stages prices quotes facts` `--as-of D` `--price-start D` | Live, max 25 symbols. Identity always runs. Idempotent. Stores raw facts only, with provenance. `data.symbols[X]` per-stage outcome; `expected_unsupported` lists IFRS issuers. |
| `universe` | `--as-of D` | What is stored: tickers, CIK, classification, last bar, last quote, fact rows, unsupported reasons. |
| `metrics` | `--symbols A B` `--as-of D` | Issuer and listing metrics with `lines` (filing/tag/accession/period) and `retrievals` provenance, `coverage`, `freshness`. |
| `screen` | `--spec`/`--spec-json` `--as-of D` `--ingest-missing` | `data.matches`, `data.failed`, `data.missing_data_exclusions`, `data.counts`, `data.universe`. Each security lists every criterion with `value`, `threshold`, `result` (PASS/FAIL/UNEVALUABLE), `flags`, `provenance`. |
| `evidence` | `--symbol A` `--as-of D` `--no-events` `--event-window-days N` | Packet with 16 sections (below). Fetches SEC submissions live unless `--no-events`. |
| `events` | `--symbols A B` `--since D` | Live and read-only: filings classified by form/8-K item, no database needed. |

Freshness (screen/metrics/evidence): `--max-price-age-days N` (default 7) turns price/quote-based metrics into `MISSING_INPUT` (`STALE_PRICE_DATA:...`) when the newest bar/quote is older than N days before `--as-of`; `--allow-stale` disables the blocking (ages are still reported).

## Evidence packet sections

`identity, price, fundamentals, valuation, balance_sheet, ownership, insiders, filings, earnings, news_events, contracts_customers, partnerships, mna, legal_regulatory, government_awards, other`.

Each section: `status`, `items`, `missing`, `guidance`.

| Section status | Meaning |
|---|---|
| `OK` | items present |
| `NONE_FOUND` | source consulted; nothing in the window (see `event_window` for the dates actually covered) |
| `NOT_COLLECTED` | the engine did not consult a source; follow `guidance` (web research with attribution) |
| `UNAVAILABLE` / `UNSUPPORTED` | cannot be provided (e.g. IFRS issuer fundamentals) |

Each item: `id, section, company{ticker,cik,name}, source, source_type, as_of, filed, content, data, provenance, caveats`.
`content` is a plain factual statement (form, date, 8-K item codes); it never interprets the filing.

## Common warning/error codes

`SEC_USER_AGENT_MISSING`, `HTTP_403` (contact not accepted; do not retry), `HTTP_429`, `NETWORK`, `NOT_IN_DATABASE` (run `ingest`), `EXPECTED_UNSUPPORTED` (IFRS), `MISSING_DATA_EXCLUSIONS`, `INVALID_SCREEN_SPEC`, `INVALID_SYMBOLS`, `DATABASE_UNAVAILABLE` (another process is writing), `EMPTY_UNIVERSE`.
