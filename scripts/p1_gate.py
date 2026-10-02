"""Re-decide the P1 progression gate from a stored P0 report, without touching any provider.

    python scripts/p1_gate.py data/p0_reports/p0_report_<stamp>.json [--tests-status passed|failed | --run-tests]

Acceptance statuses and pipeline execution are read from the report exactly as recorded and are not altered; only the
progression gate (app/ingestion/progression.py) is recomputed from the recorded criteria, the recorded integrity
validation and the automated-test result. Without a test result the gate stays CLOSED ("not reported").

Exit codes: 0 gate OPEN, 1 gate CLOSED, 2 report unusable.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingestion.p0_report import gate_lines  # noqa: E402
from app.ingestion.progression import OPEN, criteria_from_report, progression_gate  # noqa: E402


def decide(report: dict, automated_tests: str | None) -> dict:
    """The recorded verdict with its progression gate recomputed."""
    verdict = dict(report["verdict"])
    execution = verdict["pipeline_execution"]
    gate = progression_gate(
        criteria_from_report(report["acceptance"]),
        execution=execution["status"],
        genuine_failures=execution["genuine_failures"],
        expected_unsupported=execution["expected_unsupported"],
        mode=report["summary"]["mode"],
        integrity=(report.get("validation_after_run1") or {}).get("integrity"),
        automated_tests=automated_tests,
    )
    verdict["progression_gate"], verdict["p1_gate"] = gate, gate["status"]
    return verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("report", type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tests-status", choices=("passed", "failed"))
    group.add_argument("--run-tests", action="store_true", help="run `python -m pytest -q` now and use its result")
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        report["verdict"], report["acceptance"], report["summary"]  # noqa: B018 - presence check
    except (OSError, ValueError, KeyError) as exc:
        print(f"unusable report {args.report}: {exc!r}")
        return 2
    tests = args.tests_status.upper() if args.tests_status else None
    if args.run_tests:
        tests = "PASSED" if subprocess.run([sys.executable, "-m", "pytest", "-q"]).returncode == 0 else "FAILED"
    verdict = decide(report, tests)
    print(f"OVERALL P0 ACCEPTANCE: {verdict['overall']}")
    print(f"PIPELINE EXECUTION: {verdict['pipeline_execution']['status']}")
    print(f"P1 PROGRESSION GATE: {verdict['p1_gate']}\n")
    print("\n".join(gate_lines(verdict)))
    return 0 if verdict["p1_gate"] == OPEN else 1


if __name__ == "__main__":
    raise SystemExit(main())
