"""``research`` command line: the ONE entry point Claude's Skill calls. Every command prints exactly one JSON envelope
(``app.research.envelope``) on stdout and nothing else; diagnostics go to stderr.

    python research.py doctor [--network] [--deep]      environment readiness (never prints secrets)
    python research.py catalog                          screenable metrics + the screen JSON schema
    python research.py validate-screen --spec-json ...  validate a screen specification, run nothing
    python research.py resolve --symbols AAPL,MSFT      ticker -> CIK/name/exchange via SEC (live)
    python research.py ingest --symbols ... [--stages prices,quotes,facts]   fetch + store raw facts (live)
    python research.py universe                         what is stored locally
    python research.py metrics --symbols ...            canonical metrics + provenance from stored data
    python research.py screen --spec-json ...           deterministic screen over stored data
    python research.py evidence --symbol AAPL           structured evidence packet (+ live SEC filing events)
    python research.py events --symbols ...             SEC filing-event evidence (live, read-only)

Exit codes: 0 = data returned (status OK or PARTIAL), 1 = ERROR envelope, 2 = invalid arguments/specification.
SEC_USER_AGENT (``<ApplicationName> <contact email or URL>``) comes from the environment, ``--sec-user-agent`` or a
``.env`` in the working directory; it is never defaulted or invented.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings
from app.data.openbb_client import get_obb
from app.database.errors import DatabaseUnavailableError
from app.ingestion.errors import IngestionError
from app.ingestion.market_source import MarketData
from app.ingestion.sec_http import SecHttpClient
from app.ingestion.stats import RequestStats
from app.research import freshness as fr
from app.research.catalog import catalog_json
from app.research.envelope import STATUS_ERROR, STATUS_OK, STATUS_PARTIAL, make_envelope, problem
from app.research.screen_spec import ScreenSpec
from app.research.universe import MAX_INGEST_SYMBOLS, UniverseError, parse_stages, parse_symbols
from app.services import research_doctor_service as doctor_service
from app.services import research_events_service as events_service
from app.services import research_evidence_service as evidence_service
from app.services import research_ingest_service as ingest_service
from app.services import research_query_service as query_service

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2
DB_FILENAME = "research.duckdb"


class UsageError(Exception):
    """Invalid arguments or specification: reported as an ERROR envelope with exit code 2."""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.problem = problem(code, message, **details)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError("INVALID_ARGUMENTS", message)


# --- environment ---


def default_db_path() -> Path:
    """DATABASE_PATH, else <repo>/data/ when run from a checkout, else ./research_data/ (e.g. an uploaded Skill)."""
    configured = os.environ.get("DATABASE_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else Path.cwd() / path
    if (PROJECT_ROOT / "CLAUDE.md").is_file():
        return PROJECT_ROOT / "data" / DB_FILENAME
    return Path.cwd() / "research_data" / DB_FILENAME


def resolve_user_agent(flag: str | None) -> tuple[str | None, str | None]:
    """(user_agent, error). The flag wins over the environment; an invalid value is reported, never used."""
    load_dotenv(Path.cwd() / ".env", override=False)
    try:
        if flag:
            return Settings(sec_user_agent=flag).sec_user_agent, None
        return Settings.from_env().sec_user_agent, None
    except ValidationError as exc:
        return None, exc.errors()[0]["msg"]


def make_sec(user_agent: str | None, stats: RequestStats) -> SecHttpClient | None:
    return SecHttpClient(user_agent, stats) if user_agent else None


def make_market(stats: RequestStats) -> MarketData:
    return MarketData(get_obb, stats)


def today() -> date:
    return datetime.now(UTC).date()


def _now() -> datetime:
    return datetime.now(UTC)


# --- argument parsing ---


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO date (YYYY-MM-DD): {value!r}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="research", description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, help="DuckDB file (default: DATABASE_PATH, <repo>/data or ./research_data)")
    parser.add_argument("--sec-user-agent", help="'<ApplicationName> <contact email or URL>' (else SEC_USER_AGENT)")
    parser.add_argument("--pretty", action="store_true", help="indent the JSON output")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    def freshness(p: argparse.ArgumentParser) -> None:
        group = p.add_mutually_exclusive_group()
        group.add_argument(
            "--max-price-age-days",
            type=int,
            default=fr.DEFAULT_MAX_PRICE_AGE_DAYS,
            help="price/quote metrics older than this before --as-of become MISSING_INPUT (default 7)",
        )
        group.add_argument(
            "--allow-stale", action="store_true", help="do not block stale price data (age still reported)"
        )

    p = sub.add_parser("doctor", help="environment readiness")
    p.add_argument("--network", action="store_true", help="probe provider hosts and SEC access")
    p.add_argument("--deep", action="store_true", help="import OpenBB and list its providers (slow)")
    sub.add_parser("catalog", help="screenable metrics and the screen JSON schema")
    p = sub.add_parser("validate-screen", help="validate a screen specification")
    for q in (p, sub.add_parser("screen", help="run a deterministic screen over stored data")):
        group = q.add_mutually_exclusive_group(required=True)
        group.add_argument("--spec", help="path to a JSON file, or - for stdin")
        group.add_argument("--spec-json", help="the specification as a JSON string")
    screen_parser = sub.choices["screen"]
    screen_parser.add_argument("--as-of", type=_date)
    screen_parser.add_argument(
        "--ingest-missing",
        action="store_true",
        help="first ingest universe.symbols that are not stored yet (live; needs SEC_USER_AGENT)",
    )
    freshness(screen_parser)
    p = sub.add_parser("resolve", help="ticker -> CIK via SEC (live)")
    p.add_argument("--symbols", nargs="+", required=True)
    p = sub.add_parser("ingest", help="fetch and store raw data for symbols (live)")
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--stages", nargs="+", help="subset of: prices quotes facts (identity always runs)")
    p.add_argument("--as-of", type=_date)
    p.add_argument("--price-start", type=_date, help="first price date (default: the pinned pilot start 2020-01-01)")
    p = sub.add_parser("universe", help="what is stored locally")
    p.add_argument("--as-of", type=_date)
    p = sub.add_parser("metrics", help="canonical metrics for stored symbols")
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--as-of", type=_date)
    freshness(p)
    p = sub.add_parser("evidence", help="evidence packet for one stored company")
    p.add_argument("--symbol", required=True)
    p.add_argument("--as-of", type=_date)
    p.add_argument("--no-events", action="store_true", help="do not fetch SEC filing events")
    p.add_argument("--event-window-days", type=int, default=evidence_service.DEFAULT_EVENT_WINDOW_DAYS)
    freshness(p)
    p = sub.add_parser("events", help="SEC filing-event evidence (live, read-only)")
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--since", type=_date, help="earliest filing date (default: one year before today)")
    return parser


def _max_age(args: argparse.Namespace) -> int | None:
    return None if getattr(args, "allow_stale", False) else getattr(args, "max_price_age_days", None)


def _read_spec(args: argparse.Namespace) -> ScreenSpec:
    if args.spec_json is not None:
        text = args.spec_json
    elif args.spec == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(args.spec).read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError("SPEC_UNREADABLE", f"cannot read the specification file: {type(exc).__name__}") from exc
    try:
        return ScreenSpec.model_validate_json(text)
    except ValidationError as exc:
        errors = [
            {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"].removeprefix("Value error, ")}
            for e in exc.errors(include_url=False, include_input=False)
        ]
        raise UsageError(
            "INVALID_SCREEN_SPEC", f"the screen specification is invalid ({len(errors)} problem(s))", errors=errors
        ) from exc


# --- commands ---
# Each returns (data, status, warnings, errors).

Result = tuple[dict[str, Any], str, list[dict[str, Any]], list[dict[str, Any]]]


def cmd_doctor(args, ctx) -> Result:
    stats = RequestStats()
    sec = make_sec(ctx["ua"], stats) if args.network and ctx["ua"] else None
    report = doctor_service.doctor(
        ctx["db"], user_agent=ctx["ua"], user_agent_error=ctx["ua_error"], network=args.network, deep=args.deep, sec=sec
    )
    if sec:
        sec.close()
    report["engine"] = (
        "source checkout" if (PROJECT_ROOT / "CLAUDE.md").is_file() else "bundled with the packaged Skill"
    )
    problems = [
        problem(f"CHECK_{c['status']}", f"{c['name']}: {c['detail']}") for c in report["checks"] if c["status"] != "OK"
    ]
    return report, STATUS_OK if report["ready"] and not problems else STATUS_PARTIAL, problems, []


def cmd_catalog(args, ctx) -> Result:
    return (
        {
            "metrics": catalog_json(),
            "screen_schema": ScreenSpec.model_json_schema(),
            "units": "fraction metrics need unit 'percent' (15 = 15%) or 'fraction' (0.15 = 15%); others use 'native'.",
            "operators": ["gt", "gte", "lt", "lte", "eq", "between"],
            "drawdown_sign": "drawdown_from_52w_high is <= 0: 'down 30-50%' = between -50 and -30 (percent).",
        },
        STATUS_OK,
        [],
        [],
    )


def cmd_validate_screen(args, ctx) -> Result:
    spec = _read_spec(args)
    return (
        {
            "valid": True,
            "normalised_spec": spec.model_dump(mode="json"),
            "thresholds_in_stored_units": [
                {"metric": c.metric, "op": c.op, **dict(zip(("value", "low", "high"), c.scaled(), strict=True))}
                for c in spec.criteria
            ],
        },
        STATUS_OK,
        [],
        [],
    )


def cmd_resolve(args, ctx) -> Result:
    symbols = parse_symbols(args.symbols, limit=200)
    if ctx["sec"] is None:
        return (
            {},
            STATUS_ERROR,
            [],
            [problem("SEC_USER_AGENT_MISSING", ctx["ua_error"] or "SEC_USER_AGENT is not configured")],
        )
    result = events_service.resolve_symbols(ctx["sec"], list(symbols))
    status = STATUS_PARTIAL if result["unresolved"] else STATUS_OK
    return (
        result,
        status,
        [problem("UNRESOLVED_SYMBOL", f"{s}: {u['message']}") for s, u in result["unresolved"].items()],
        [],
    )


def cmd_ingest(args, ctx) -> Result:
    symbols = list(parse_symbols(args.symbols, limit=MAX_INGEST_SYMBOLS))
    stages = parse_stages(args.stages)
    kwargs = {"price_start": args.price_start} if args.price_start else {}
    summary = ingest_service.ingest_symbols(
        ctx["db"],
        symbols,
        as_of=args.as_of or today(),
        sec=ctx["sec"],
        market=ctx["market"],
        stats=ctx["stats"],
        stages=stages,
        raw_dir=ctx["db"].parent / "raw",
        **kwargs,
    )
    if summary["fatal"]:
        return summary, STATUS_ERROR, [], [problem(summary["fatal"], f"ingestion could not run ({summary['fatal']})")]
    warnings = [problem(i["kind"], f"{i['stage']} {i['symbol']}: {i['message']}") for i in summary["issues"]]
    warnings += [
        problem("EXPECTED_UNSUPPORTED", e["message"], symbol=e["symbol"]) for e in summary["expected_unsupported"]
    ]
    return summary, STATUS_PARTIAL if warnings else STATUS_OK, warnings, []


def _require_db(ctx) -> None:
    if not ctx["db"].is_file():
        raise UsageError("NO_DATABASE", "no local database yet: run `ingest --symbols ...` first")


def cmd_universe(args, ctx) -> Result:
    _require_db(ctx)
    return query_service.universe_overview(ctx["db"], args.as_of or today()), STATUS_OK, [], []


def cmd_metrics(args, ctx) -> Result:
    _require_db(ctx)
    symbols = list(parse_symbols(args.symbols, limit=200))
    data = query_service.metrics_for(ctx["db"], symbols, args.as_of or today(), max_price_age_days=_max_age(args))
    warnings = [problem("NOT_IN_DATABASE", f"{s} is not stored: run ingest first") for s in data["not_in_database"]]
    return data, STATUS_PARTIAL if warnings or not data["securities"] else STATUS_OK, warnings, []


def cmd_screen(args, ctx) -> Result:
    spec = _read_spec(args)
    warnings: list[dict[str, Any]] = []
    if args.ingest_missing:
        if not spec.universe.symbols:
            raise UsageError(
                "INGEST_MISSING_NEEDS_SYMBOLS", "--ingest-missing needs universe.symbols in the specification"
            )
        stored = query_service.stored_tickers(ctx["db"])
        todo = [s for s in spec.universe.symbols if s not in stored]
        if todo:
            res = cmd_ingest(
                argparse.Namespace(symbols=todo, stages=None, as_of=args.as_of or spec.as_of, price_start=None), ctx
            )
            warnings += res[2] + res[3]
            if res[1] == STATUS_ERROR:
                return (
                    {"ingest": res[0]},
                    STATUS_ERROR,
                    warnings,
                    [problem("INGEST_FAILED", "could not ingest the missing symbols; screen not run")],
                )
    _require_db(ctx)
    data = query_service.screen(ctx["db"], spec, args.as_of or today(), max_price_age_days=_max_age(args))
    counts = data["counts"]
    if data["universe"]["requested_but_not_in_database"]:
        warnings.append(
            problem(
                "NOT_IN_DATABASE",
                "some requested symbols are not stored and were not screened",
                symbols=data["universe"]["requested_but_not_in_database"],
            )
        )
    if counts["missing_data_exclusions"]:
        warnings.append(
            problem(
                "MISSING_DATA_EXCLUSIONS", f"{counts['missing_data_exclusions']} security(ies) could not be evaluated"
            )
        )
    if counts["screened"] == 0:
        return (
            data,
            STATUS_ERROR,
            warnings,
            [problem("EMPTY_UNIVERSE", "no stored securities to screen: run ingest first")],
        )
    return data, STATUS_PARTIAL if warnings else STATUS_OK, warnings, []


def cmd_evidence(args, ctx) -> Result:
    _require_db(ctx)
    (symbol,) = parse_symbols([args.symbol], limit=1)
    packet = evidence_service.build_packet(
        ctx["db"],
        symbol,
        args.as_of or today(),
        sec=ctx["sec"],
        now=_now(),
        collect_events=not args.no_events,
        event_window_days=args.event_window_days,
        max_price_age_days=_max_age(args),
    )
    if packet is None:
        return (
            {},
            STATUS_ERROR,
            [],
            [problem("NOT_IN_DATABASE", f"{symbol} is not stored: run `ingest --symbols {symbol}` first")],
        )
    sections = packet["sections"]
    not_collected = sorted(
        n for n, s in sections.items() if s["status"] in ("NOT_COLLECTED", "UNAVAILABLE", "UNSUPPORTED")
    )
    warnings = [problem("SECTION_" + sections[n]["status"], f"{n}: {sections[n]['status']}") for n in not_collected]
    return packet, STATUS_PARTIAL if warnings else STATUS_OK, warnings, []


def cmd_events(args, ctx) -> Result:
    symbols = list(parse_symbols(args.symbols, limit=MAX_INGEST_SYMBOLS))
    if ctx["sec"] is None:
        return (
            {},
            STATUS_ERROR,
            [],
            [problem("SEC_USER_AGENT_MISSING", ctx["ua_error"] or "SEC_USER_AGENT is not configured")],
        )
    now = _now()
    data = events_service.events_report(
        ctx["sec"], symbols, since=args.since or (now.date() - timedelta(days=365)), now=now
    )
    bad = [s for s, c in data["companies"].items() if c["status"] != "OK"] + list(data["unresolved"])
    return (
        data,
        STATUS_PARTIAL if bad else STATUS_OK,
        [problem("EVENTS_UNAVAILABLE", f"{s}: not available") for s in bad],
        [],
    )


COMMANDS: dict[str, Callable[..., Result]] = {
    "doctor": cmd_doctor,
    "catalog": cmd_catalog,
    "validate-screen": cmd_validate_screen,
    "resolve": cmd_resolve,
    "ingest": cmd_ingest,
    "universe": cmd_universe,
    "metrics": cmd_metrics,
    "screen": cmd_screen,
    "evidence": cmd_evidence,
    "events": cmd_events,
}
LIVE_COMMANDS = {"resolve", "ingest", "events", "evidence", "screen", "doctor"}


def run(argv: Sequence[str] | None = None) -> tuple[dict[str, Any], int]:
    """Parse and execute; returns (envelope, exit code). Never raises for expected failures."""
    command = "unknown"
    try:
        args = build_parser().parse_args(argv)
        command = args.command
        ua, ua_error = resolve_user_agent(args.sec_user_agent)
        stats = RequestStats()
        ctx: dict[str, Any] = {
            "db": args.db or default_db_path(),
            "ua": ua,
            "ua_error": ua_error,
            "stats": stats,
            "sec": make_sec(ua, stats) if command in LIVE_COMMANDS - {"doctor"} else None,
            "market": make_market(stats) if command in ("ingest", "screen") else None,
        }
        try:
            data, status, warnings, errors = COMMANDS[command](args, ctx)
        finally:
            if ctx["sec"] is not None:
                ctx["sec"].close()
        return make_envelope(command, data, status=status, warnings=warnings, errors=errors), (
            EXIT_ERROR if status == STATUS_ERROR else EXIT_OK
        )
    except UsageError as exc:
        return make_envelope(command, {}, status=STATUS_ERROR, errors=[exc.problem]), EXIT_USAGE
    except UniverseError as exc:
        return make_envelope(
            command, {}, status=STATUS_ERROR, errors=[problem("INVALID_SYMBOLS", str(exc))]
        ), EXIT_USAGE
    except DatabaseUnavailableError as exc:
        return make_envelope(
            command, {}, status=STATUS_ERROR, errors=[problem("DATABASE_UNAVAILABLE", str(exc))]
        ), EXIT_ERROR
    except IngestionError as exc:
        return make_envelope(command, {}, status=STATUS_ERROR, errors=[problem(exc.kind, str(exc))]), EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - a command must always answer with an envelope, never a traceback
        message = f"{type(exc).__name__}: {exc}"
        return make_envelope(
            command, {}, status=STATUS_ERROR, errors=[problem("UNEXPECTED_ERROR", message)]
        ), EXIT_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    envelope, code = run(argv)
    pretty = "--pretty" in (argv if argv is not None else sys.argv[1:])
    sys.stdout.write(json.dumps(envelope, indent=2 if pretty else None, ensure_ascii=False, allow_nan=False) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
