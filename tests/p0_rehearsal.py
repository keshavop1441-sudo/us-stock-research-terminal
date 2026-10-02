"""Offline REHEARSAL of the P0 sequence on SIMULATED providers (tests/p0_fakes.py): proves the mechanics, not provider
behaviour.

    python tests/p0_rehearsal.py [--out-dir DIR]

Writes ``p0_rehearsal_report.json`` / ``.md`` stamped SIMULATED. It is not a substitute for ``scripts/run_p0.py``.
"""

import argparse
import sys
import tempfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import p0_fakes as fk  # noqa: E402

from app.database.connection import ensure_database  # noqa: E402
from app.ingestion.market_source import MarketData  # noqa: E402
from app.ingestion.p0_report import render_markdown  # noqa: E402
from app.ingestion.run_report import write_json  # noqa: E402
from app.ingestion.sec_http import SecHttpClient  # noqa: E402
from app.ingestion.stats import RequestStats  # noqa: E402
from app.services.ingestion_service import P0Run, run_p0_sequence  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=Path(tempfile.mkdtemp(prefix="p0_rehearsal_")))
    args = parser.parse_args()
    as_of = date(2026, 2, 13)
    db = args.out_dir / "rehearsal.duckdb"
    ensure_database(db)
    sec, obb = fk.FakeSec(), fk.FakeObb(as_of)

    def make_run(run_id: str) -> P0Run:
        stats = RequestStats()
        client = SecHttpClient(
            "RehearsalApp rehearsal@example.org",
            stats,
            transport=sec.transport(),
            min_interval=0.12,
            sleep=lambda s: None,
        )
        return P0Run(
            db, run_id=run_id, mode="SIMULATED", as_of=as_of, sec=client, market=MarketData(lambda: obb, stats),
            stats=stats, raw_dir=args.out_dir / "raw",
        )  # fmt: skip

    report = run_p0_sequence(db, make_run)
    write_json(args.out_dir / "p0_rehearsal_report.json", report)
    (args.out_dir / "p0_rehearsal_report.md").write_text(render_markdown(report), encoding="utf-8")
    print(f"wrote {args.out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
