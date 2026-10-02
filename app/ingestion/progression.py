"""The P1 progression gate: "is any unresolved P0 blocker preventing the start of P1?"

This is a DECISION LAYER ON TOP OF the acceptance criteria (``acceptance.py``), never a replacement for them:

  * acceptance status  - A1-A13 derived strictly from their required components. Untouched by anything here.
  * pipeline execution - did the run complete, with how many genuine failures (``acceptance.overall_verdict``).
  * progression gate   - this module. OPEN / CLOSED, deterministic, with every reason stated.

A component that is not PASS keeps its honest status (DEFERRED / NOT_EVALUABLE ...) and the criterion stays PARTIAL.
Whether such a component blocks P1 is decided ONLY by an explicit, auditable ``ComponentPolicy`` in ``POLICIES``:
no policy entry means it blocks (fail closed). Nothing is special-cased in code.

The gate is OPEN only if ALL of these hold:
  1. the pipeline executed to completion (COMPLETED) with 0 genuine failures;
  2. the run was LIVE (a simulated run proves mechanics only);
  3. no data-integrity violation was counted in the stored data (every ``integrity_report`` count is 0);
  4. the required automated tests were reported as PASSED (not reported counts as not passed);
  5. no criterion component is FAIL (a measured miss always blocks, whatever its policy), and every component that is
     not PASS has a policy with ``blocking_for_p1_entry = False`` (and a status the policy was written for).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.ingestion.acceptance import (
    DEFERRED,
    EXPECTED_UNSUPPORTED,
    FAIL,
    NOT_EVALUABLE,
    PASS,
    Component,
    Criterion,
)

OPEN, CLOSED = "OPEN", "CLOSED"
TESTS_PASSED, TESTS_FAILED = "PASSED", "FAILED"
DEFERRAL_TARGETS = frozenset({"P1", "P2", "FUTURE"})
_WAIVABLE = frozenset({DEFERRED, NOT_EVALUABLE, EXPECTED_UNSUPPORTED})  # statuses a policy may speak for (never FAIL)
QUESTION = "Are there any unresolved P0 blockers that prevent beginning P1?"


@dataclass(frozen=True)
class ComponentPolicy:
    """Why a non-PASS component does or does not block P1 entry.

    blocking_for_p0_completion  P0 may not be called ACCEPTED while this component is not PASS (always True today: the
                                acceptance statuses are strict and the policy cannot change them).
    blocking_for_p1_entry       the component being unevaluated keeps the P1 gate CLOSED.
    deferred_to                 "P1" / "P2" / "FUTURE" when the measurement belongs to a later stage, else None.
    evidence_source             what stands in for the missing live observation: ``path::test_name`` entries must name
                                an existing test (tests/test_p0_progression.py checks), other entries are documents.
    live_trigger_required       the live run cannot produce this observation on demand (needs a real provider event).
    """

    criterion: str
    component: str
    blocking_for_p0_completion: bool
    blocking_for_p1_entry: bool
    deferred_to: str | None
    evidence_source: tuple[str, ...]
    live_trigger_required: bool
    reason: str

    def __post_init__(self) -> None:
        if self.deferred_to is not None and self.deferred_to not in DEFERRAL_TARGETS:
            raise ValueError(f"unknown deferral target {self.deferred_to!r}")
        if not self.reason or not self.evidence_source:
            raise ValueError("a policy must state its reason and its evidence source")


_PIPELINE_TESTS = "tests/test_p0_pipeline.py"
_COVERAGE_YAML = "docs/data_coverage.yaml#universe_pilot"

POLICIES: tuple[ComponentPolicy, ...] = (
    ComponentPolicy(
        "A3",
        "split_rebase_true_detection",
        True,
        False,
        "P1",
        (
            f"{_PIPELINE_TESTS}::test_a_split_rebase_of_the_provider_history_is_detected_and_rewrites_rather_than_duplicates",
        ),
        True,
        "A real split cannot be triggered on demand; the live run proves the no-false-positive side and the hermetic "
        "test proves detection and rewrite. The spec's 'known split names' sample belongs to the P1 pilot stratum.",
    ),
    ComponentPolicy(
        "A4",
        "total_assets",
        True,
        False,
        "P1",
        (_COVERAGE_YAML,),
        False,
        "total_assets is not in the P0 concept set: known deferred coverage, not a pipeline failure. It must be "
        "ingested and measured before the P1 pilot can pass A4 (scaling_rule: P1 -> P2 needs every A1-A13).",
    ),
    ComponentPolicy(
        "A7",
        "full_universe_projection",
        True,
        False,
        "P1",
        (_COVERAGE_YAML,),
        False,
        "Full-universe request/time projection needs P1 measurements; P0 is 13 issuers.",
    ),
    ComponentPolicy(
        "A10",
        "extrapolation_meets_a7",
        True,
        False,
        "P2",
        (_COVERAGE_YAML,),
        False,
        "Full-universe extrapolation needs P1/P2 measurements (scaling_rule: P2 -> full universe).",
    ),
    ComponentPolicy(
        "A11",
        "changed_fact_updates_one",
        True,
        False,
        None,
        (f"{_PIPELINE_TESTS}::test_one_changed_source_value_updates_exactly_one_row_and_a_new_accession_adds_one",),
        True,
        "A live run cannot change a provider value on demand; the live re-run proves idempotency and the hermetic test "
        "proves exactly one row is updated.",
    ),
    ComponentPolicy(
        "A11",
        "amendment_adds_accession",
        True,
        False,
        None,
        (f"{_PIPELINE_TESTS}::test_one_changed_source_value_updates_exactly_one_row_and_a_new_accession_adds_one",),
        True,
        "A live run cannot produce a new amendment on demand; the hermetic test proves a new accession adds a row.",
    ),
    ComponentPolicy(
        "A11",
        "reads_honest_during_write",
        True,
        False,
        None,
        (
            "tests/test_service_db.py::test_reading_during_a_refresh_fails_clearly",
            "tests/test_research_cli.py::test_reads_during_a_refresh_are_unavailable_never_stale_data",
        ),
        True,
        "Read-during-write needs a concurrent writer; the service and CLI tests prove reads report 'unavailable' "
        "(never stale data as current) while a refresh holds the database.",
    ),
    ComponentPolicy(
        "A12",
        "revenue_vs_nasdaq",
        True,
        False,
        "FUTURE",
        (_COVERAGE_YAML,),
        False,
        "Nasdaq statements are not ingested, so the SEC-vs-Nasdaq revenue comparison is outside P0. The 52-week-high "
        "and market-cap cross-checks are measured separately and must still PASS.",
    ),
)
POLICY_INDEX: Mapping[tuple[str, str], ComponentPolicy] = {(p.criterion, p.component): p for p in POLICIES}
if len(POLICY_INDEX) != len(POLICIES):  # pragma: no cover - guards a copy/paste slip in the table above
    raise ValueError("duplicate component policy")


def _policy_view(p: ComponentPolicy, c: Component) -> dict[str, object]:
    return {
        "criterion": p.criterion, "component": p.component, "status": c.status, "observed": c.observed,
        "blocking_for_p0_completion": p.blocking_for_p0_completion, "blocking_for_p1_entry": p.blocking_for_p1_entry,
        "deferred_to": p.deferred_to, "evidence_source": list(p.evidence_source),
        "live_trigger_required": p.live_trigger_required, "reason": p.reason,
    }  # fmt: skip


def progression_gate(
    criteria: Sequence[Criterion],
    *,
    execution: str,
    genuine_failures: Sequence[Mapping],
    expected_unsupported: Sequence[Mapping],
    mode: str | None,
    integrity: Mapping[str, int] | None,
    automated_tests: str | None,
    policies: Mapping[tuple[str, str], ComponentPolicy] = POLICY_INDEX,
) -> dict[str, object]:
    """Deterministic OPEN / CLOSED decision with every blocker, deferral and PARTIAL explained."""
    blockers: list[dict[str, str]] = []

    def block(source: str, reason: str) -> None:
        blockers.append({"source": source, "reason": reason})

    if execution != "COMPLETED":
        block("pipeline_execution", f"pipeline execution is {execution}, not COMPLETED")
    if genuine_failures:
        block("genuine_failures", f"{len(genuine_failures)} genuine pipeline failure(s)")
    if mode != "LIVE":
        block("live_run", f"run mode is {mode!r}: only a LIVE run can open the gate")
    violations = {k: v for k, v in (integrity or {}).items() if v}
    if not integrity:
        block("data_integrity", "no data-integrity validation was reported")
    elif violations:
        block("data_integrity", f"critical data-integrity violation(s): {violations}")
    if automated_tests != TESTS_PASSED:
        block("automated_tests", f"required automated tests: {automated_tests or 'not reported'} (need PASSED)")

    deferred: list[dict[str, object]] = []
    per_criterion: list[dict[str, object]] = []
    for crit in criteria:
        crit_blockers: list[str] = []
        crit_deferred: list[str] = []
        for comp in crit.components:
            if comp.status == PASS:
                continue
            key = f"{crit.id}.{comp.name}"
            policy = policies.get((crit.id, comp.name))
            if comp.status == FAIL:
                crit_blockers.append(f"{key} FAILED")
                block(key, "component FAILED (a measured miss always blocks)")
            elif policy is None:
                crit_blockers.append(f"{key} is {comp.status} with no policy")
                block(key, f"{comp.status} and no explicit progression policy (fail closed)")
            elif comp.status not in _WAIVABLE:  # pragma: no cover - statuses are a closed set
                crit_blockers.append(f"{key} has unexpected status {comp.status}")
                block(key, f"unexpected status {comp.status}")
            elif policy.blocking_for_p1_entry:
                crit_blockers.append(f"{key} is {comp.status} and blocks P1 entry")
                block(key, f"{comp.status}; policy marks it blocking_for_p1_entry: {policy.reason}")
            else:
                crit_deferred.append(key)
                deferred.append(_policy_view(policy, comp))
        if crit.status != PASS:
            per_criterion.append({
                "id": crit.id, "status": crit.status, "blocks_p1": bool(crit_blockers),
                "reason": ("blocks P1: " + "; ".join(crit_blockers)) if crit_blockers else
                          ("does not block P1: non-PASS components are explicitly non-blocking by policy: "
                           + "; ".join(crit_deferred)),
            })  # fmt: skip

    return {
        "status": CLOSED if blockers else OPEN,
        "question": QUESTION,
        "conditions": {
            "pipeline_execution": execution,
            "genuine_failures": len(genuine_failures),
            "live_run": mode == "LIVE",
            "data_integrity_violations": violations if integrity else None,
            "automated_tests": automated_tests or "NOT_REPORTED",
            "blocking_components": [b["source"] for b in blockers if "." in b["source"]],
        },
        "blockers": blockers,
        "deferred_items": deferred,
        "partial_criteria": per_criterion,
        "expected_unsupported": [dict(e) for e in expected_unsupported],
    }


def criteria_from_report(acceptance: Sequence[Mapping]) -> list[Criterion]:
    """Rebuild criteria from a stored report's ``acceptance`` list, to re-decide the gate without re-running live."""
    return [
        Criterion(
            a["id"], a["measurement"], a["threshold"],
            [Component(c["name"], c["status"], c["observed"], c.get("note", "")) for c in a["components"]],
            list(a.get("informational", [])), a.get("note", ""),
        )
        for a in acceptance
    ]  # fmt: skip
