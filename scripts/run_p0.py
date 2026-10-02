"""Phase 3A P0 ingestion pilot: LIVE run against SEC (companyfacts/submissions), Cboe and Nasdaq (through OpenBB V5).

    python scripts/run_p0.py [--db data/p0_pilot.duckdb] [--out-dir data/p0_reports] [--as-of YYYY-MM-DD] [--once]

Order (brief section 19): apply the schema migration, ingest the 14 P0 securities, validate the stored data, ingest
the SAME thing again (skip with --once), validate idempotency, write the report (JSON + Markdown). It never starts
P1/P2.

``SEC_USER_AGENT`` (``<ApplicationName> <contact email or URL>``) is required. If it is missing the run does not
start, no request is made, and the report says so: no placeholder contact is ever substituted. The pilot database is a
separate file so it can be discarded; raw provider payloads are kept beside it for audit.

Exit codes: 0 completed, 1 completed with failures recorded, 2 database busy, 3 SEC_USER_AGENT missing/invalid.
"""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402

from app.config import PROJECT_ROOT, Settings  # noqa: E402
from app.data.openbb_client import get_obb  # noqa: E402
from app.database.connection import ensure_database  # noqa: E402
from app.ingestion.market_source import MarketData  # noqa: E402
from app.ingestion.p0_report import render_markdown  # noqa: E402
from app.ingestion.run_report import write_json  # noqa: E402
from app.ingestion.sec_http import SecHttpClient  # noqa: E402
from app.ingestion.stats import RequestStats  # noqa: E402
from app.services.ingestion_service import P0Run, run_p0_sequence  # noqa: E402

EXIT_BUSY, EXIT_NO_USER_AGENT = 2, 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data" / "p0_pilot.duckdb")
    parser.add_argument("--out-dir", type=Path, default=PROJECT_ROOT / "data" / "p0_reports")
    parser.add_argument(
        "--as-of", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), default=datetime.now(UTC).date()
    )
    parser.add_argument("--once", action="store_true", help="skip the second (idempotency) run")
    args = parser.parse_args(argv)

    try:
        user_agent = Settings.from_env().sec_user_agent
    except ValidationError as exc:
        print(f"SEC_USER_AGENT is invalid: {exc.errors()[0]['msg']}")
        return EXIT_NO_USER_AGENT
    if not user_agent:
        print("SEC_USER_AGENT is not set: live SEC ingestion cannot proceed. No request will be made.")
    version = ensure_database(args.db)
    print(f"Pilot database: {args.db} (schema v{version})")

    def make_run(run_id: str) -> P0Run:
        stats = RequestStats()
        sec = SecHttpClient(user_agent, stats) if user_agent else None
        return P0Run(
            args.db, run_id=run_id, mode="LIVE", as_of=args.as_of, sec=sec,
            market=MarketData(get_obb, stats), stats=stats, raw_dir=args.db.parent / "p0_raw",
        )  # fmt: skip

    report = run_p0_sequence(args.db, make_run, second_run=not args.once)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    write_json(args.out_dir / f"p0_report_{stamp}.json", report)
    md_path = args.out_dir / f"p0_report_{stamp}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"Report: {md_path}")
    fatal = report["summary"].get("fatal")  # type: ignore[union-attr]
    if fatal == "SEC_USER_AGENT_MISSING":
        return EXIT_NO_USER_AGENT
    if fatal == "DATABASE_LOCKED":
        return EXIT_BUSY
    return 1 if (fatal or report["issues"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
