"""Acceptance statuses for the P0 report: a criterion is the sum of its REQUIRED components, never more.

Component status (what happened to ONE required measurement):
  PASS                  measured and met the threshold
  FAIL                  measured and missed the threshold
  NOT_EVALUABLE         could not be measured in this run (simulated providers, no second run, manual review ...)
  DEFERRED              cannot be measured yet because the data was intentionally not ingested / needs a later stage
  EXPECTED_UNSUPPORTED  the issuer or data class is intentionally outside the supported coverage

Criterion status is DERIVED from its required components (``derive_status``); nobody sets it by hand:
  FAIL                  any required component FAILED (a measured miss is decisive)
  PASS                  EVERY required component PASSED
  EXPECTED_UNSUPPORTED  every required component is EXPECTED_UNSUPPORTED
  PARTIAL               at least one component PASSED but at least one other was not PASS (not evaluated or unsupported)
  DEFERRED / NOT_EVALUABLE  nothing passed: DEFERRED if any component is deferred, else NOT_EVALUABLE
So a criterion can only be PASS when no required measurement is missing: an unevaluated component can never be PASS.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

PASS, FAIL, PARTIAL = "PASS", "FAIL", "PARTIAL"
NOT_EVALUABLE, DEFERRED, EXPECTED_UNSUPPORTED = "NOT_EVALUABLE", "DEFERRED", "EXPECTED_UNSUPPORTED"
COMPONENT_STATUSES = frozenset({PASS, FAIL, NOT_EVALUABLE, DEFERRED, EXPECTED_UNSUPPORTED})
CRITERION_STATUSES = COMPONENT_STATUSES | {PARTIAL}


@dataclass
class Component:
    name: str
    status: str
    observed: str
    note: str = ""

    def __post_init__(self) -> None:
        if self.status not in COMPONENT_STATUSES:
            raise ValueError(f"unknown component status {self.status!r}")


def component(
    name: str, ok: bool | None, observed: str, note: str = "", *, deferred: bool = False, unsupported: bool = False
) -> Component:
    """``ok`` True -> PASS, False -> FAIL, None -> not measured (DEFERRED / EXPECTED_UNSUPPORTED / NOT_EVALUABLE)."""
    if ok is True:
        return Component(name, PASS, observed, note)
    if ok is False:
        return Component(name, FAIL, observed, note)
    status = EXPECTED_UNSUPPORTED if unsupported else (DEFERRED if deferred else NOT_EVALUABLE)
    return Component(name, status, observed, note)


def derive_status(statuses: Iterable[str]) -> str:
    states = list(statuses)
    if not states:
        return NOT_EVALUABLE  # a criterion with no required component has measured nothing
    if FAIL in states:
        return FAIL
    if all(s == PASS for s in states):
        return PASS
    if all(s == EXPECTED_UNSUPPORTED for s in states):
        return EXPECTED_UNSUPPORTED
    if PASS in states:
        return PARTIAL
    return DEFERRED if DEFERRED in states else NOT_EVALUABLE


@dataclass
class Criterion:
    id: str
    measurement: str
    threshold: str
    components: list[Component]  # REQUIRED components only
    informational: list[str] = field(default_factory=list)  # reported alongside, never part of the status
    note: str = ""

    @property
    def status(self) -> str:
        return derive_status(c.status for c in self.components)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "measurement": self.measurement,
            "threshold": self.threshold,
            "status": self.status,
            "observed": "; ".join(f"{c.name}: {c.observed}" for c in self.components),
            "note": self.note,
            "components": [c.__dict__ for c in self.components],
            "informational": self.informational,
        }


def not_run(criterion_id: str, reason: str) -> Criterion:
    return Criterion(
        criterion_id, "-", "-", [Component("run_did_not_proceed", NOT_EVALUABLE, "not measured", reason)], note=reason
    )


# --- overall verdict -----------------------------------------------------------------------------------------------

STATIC_LIMITATIONS = (
    {"id": "NASDAQ_REVENUE_NOT_INGESTED", "affects": "A12",
     "text": "Nasdaq statements are not ingested, so the SEC-vs-Nasdaq revenue cross-check cannot be measured."},
    {"id": "TOTAL_ASSETS_NOT_IN_CONCEPT_SET", "affects": "A4",
     "text": "Total assets is not in the P0 concept set, so the A4 'total assets' requirement is not measured."},
    {"id": "SCALE_PROJECTIONS_NEED_P1", "affects": "A7, A10",
     "text": "Full-universe request/time projections need P1 measurements; P0 is 13 issuers."},
    {"id": "NO_SECURITY_TYPE_SHARE_CLASS_TICKER_HISTORY", "affects": "identity",
     "text": "security type, share class and ticker history are not stored (providers do not supply them)."},
    {"id": "SUBMISSIONS_RECENT_ONLY", "affects": "filings",
     "text": "Only filings.recent of each submissions document is indexed; older amendments are not."},
)  # fmt: skip


def known_limitations(expected_unsupported: Sequence[dict]) -> list[dict[str, str]]:
    out = [
        {
            "id": f"UNSUPPORTED_TAXONOMY:{e['symbol']}",
            "affects": "fundamental screens",
            "text": f"{', '.join(e.get('symbols') or [e['symbol']])} (CIK {e['cik']}): {e['message']}. It cannot "
            "participate in any screen that needs accounting facts: those metrics are MISSING_INPUT "
            f"(UNSUPPORTED_TAXONOMY:{e['taxonomy']}), never 0. Identity, prices, quotes and classification are loaded.",
        }
        for e in expected_unsupported
    ]
    return out + [dict(x) for x in STATIC_LIMITATIONS]


def overall_verdict(
    *,
    run_fatal: str | None,
    genuine_failures: Sequence[dict],
    expected_unsupported: Sequence[dict],
    securities_attempted: int,
    succeeded_all_stages: int,
    criteria: Sequence[Criterion],
    second_run_failures: int | None = None,
) -> dict[str, object]:
    """Reconcile execution, acceptance and coverage into one verdict. ``ACCEPTED`` needs ALL of: the run executed, no
    genuine failure, and every criterion PASS. Known limitations are listed but cannot hide an unmet criterion.
    The P1 progression gate is a separate decision layer (``progression.py``) and does not alter any status here."""
    if run_fatal:
        execution = "ABORTED" if run_fatal in {"DATABASE_ERROR", "DATABASE_LOCKED"} else "DID_NOT_RUN"
    elif genuine_failures:
        execution = "COMPLETED_WITH_FAILURES"
    else:
        execution = "COMPLETED"
    counts: dict[str, int] = {}
    for c in criteria:
        counts[c.status] = counts.get(c.status, 0) + 1
    not_passed = [{"id": c.id, "status": c.status} for c in criteria if c.status != PASS]
    if execution == "DID_NOT_RUN":
        acceptance = "NOT_RUN"
    elif counts.get(FAIL):
        acceptance = "FAILED"
    elif not_passed:
        acceptance = "INCOMPLETE"
    else:
        acceptance = "ACCEPTED"
    if acceptance == "NOT_RUN":
        overall = "NOT_RUN"
    elif execution != "COMPLETED" or acceptance == "FAILED":
        overall = "FAILED"
    elif acceptance == "INCOMPLETE":
        overall = "INCOMPLETE"
    else:
        overall = "ACCEPTED"
    reasons: list[str] = []
    if execution != "COMPLETED":
        reasons.append(f"pipeline execution: {execution}")
    reasons += [f"{x['id']} is {x['status']}" for x in not_passed]
    return {
        "overall": overall,
        "pipeline_execution": {
            "status": execution,
            "securities_attempted": securities_attempted,
            "succeeded_all_stages": succeeded_all_stages,
            "genuine_failures": list(genuine_failures),
            "expected_unsupported": [
                {"symbol": e["symbol"], "cik": e["cik"], "kind": e["kind"], "taxonomy": e.get("taxonomy")}
                for e in expected_unsupported
            ],
            "rerun_failures": second_run_failures,
        },
        "acceptance_criteria": {"status": acceptance, "counts": counts, "not_passed": not_passed},
        "known_coverage_limitations": known_limitations(expected_unsupported),
        "acceptance_reasons": reasons,  # why the overall verdict is not ACCEPTED; the P1 gate is progression.py
    }
