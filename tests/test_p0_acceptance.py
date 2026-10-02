"""Acceptance semantics (pure): a criterion is PASS only if EVERY required component PASSED.

PASS / FAIL / PARTIAL / NOT_EVALUABLE / DEFERRED / EXPECTED_UNSUPPORTED, derived from components and never set by hand,
plus the reconciliation of pipeline execution, acceptance criteria and known coverage limitations into one verdict.
"""

import itertools

import pytest

from app.ingestion import acceptance as acc
from app.ingestion.acceptance import (
    DEFERRED,
    EXPECTED_UNSUPPORTED,
    FAIL,
    NOT_EVALUABLE,
    PARTIAL,
    PASS,
    Component,
    Criterion,
    component,
    derive_status,
    overall_verdict,
)
from app.ingestion.p0_report import REQUIRED_COMPONENTS


def crit(*statuses, cid="AX"):
    return Criterion(cid, "m", "t", [Component(f"c{i}", s, "obs") for i, s in enumerate(statuses)])


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([PASS, PASS], PASS),
        ([PASS, FAIL], FAIL),
        ([FAIL, NOT_EVALUABLE], FAIL),  # a measured miss is decisive even when something else was not measured
        ([PASS, NOT_EVALUABLE], PARTIAL),
        ([PASS, DEFERRED], PARTIAL),
        ([PASS, DEFERRED, NOT_EVALUABLE], PARTIAL),
        ([PASS, EXPECTED_UNSUPPORTED], PARTIAL),  # the unsupported portion was not demonstrated
        ([NOT_EVALUABLE, NOT_EVALUABLE], NOT_EVALUABLE),
        ([DEFERRED, NOT_EVALUABLE], DEFERRED),
        ([DEFERRED], DEFERRED),
        ([EXPECTED_UNSUPPORTED, EXPECTED_UNSUPPORTED], EXPECTED_UNSUPPORTED),
        ([], NOT_EVALUABLE),  # nothing required, nothing measured
    ],
)
def test_status_is_derived_from_the_required_components(statuses, expected):
    assert derive_status(statuses) == expected
    assert crit(*statuses).status == expected


def test_a_criterion_is_pass_if_and_only_if_every_component_passed_exhaustively():
    """No combination of up to four component statuses lets an unevaluated component produce PASS."""
    for n in range(0, 5):
        for combo in itertools.product(sorted(acc.COMPONENT_STATUSES), repeat=n):
            status = derive_status(combo)
            assert (status == PASS) == (n > 0 and all(s == PASS for s in combo)), combo
            if any(s == FAIL for s in combo):
                assert status == FAIL
            if status == PARTIAL:
                assert PASS in combo and any(s != PASS for s in combo)


def test_an_unmeasured_component_can_never_be_built_as_a_pass():
    assert component("x", None, "o").status == NOT_EVALUABLE
    assert component("x", None, "o", deferred=True).status == DEFERRED
    assert component("x", None, "o", unsupported=True).status == EXPECTED_UNSUPPORTED
    assert component("x", True, "o").status == PASS and component("x", False, "o").status == FAIL
    with pytest.raises(ValueError):
        Component("x", "MAYBE", "o")


def test_the_serialised_criterion_carries_status_and_every_component():
    data = crit(PASS, DEFERRED).to_dict()
    assert data["status"] == PARTIAL and [c["status"] for c in data["components"]] == [PASS, DEFERRED]
    assert "c1: obs" in data["observed"]


def test_every_criterion_has_a_pinned_component_list():
    assert sorted(REQUIRED_COMPONENTS, key=lambda k: int(k[1:])) == [f"A{i}" for i in range(1, 14)]
    assert all(len(v) >= 2 for v in REQUIRED_COMPONENTS.values())
    assert "revenue_vs_nasdaq" in REQUIRED_COMPONENTS["A12"]  # the Nasdaq revenue clause cannot be dropped silently


# --- overall verdict -------------------------------------------------------------------------------------------------

UNSUPPORTED = [{"symbol": "TSM", "symbols": ["TSM"], "cik": "0001046179", "kind": "UNSUPPORTED_TAXONOMY",
                "taxonomy": "ifrs-full", "message": "companyfacts uses 'ifrs-full' only"}]  # fmt: skip


def verdict(criteria, *, failures=(), unsupported=(), fatal=None, succeeded=14):
    return overall_verdict(
        run_fatal=fatal, genuine_failures=list(failures), expected_unsupported=list(unsupported),
        securities_attempted=14, succeeded_all_stages=succeeded, criteria=criteria,
    )  # fmt: skip


def test_all_pass_and_no_failure_is_accepted_and_opens_the_gate():
    v = verdict([crit(PASS, PASS, cid=f"A{i}") for i in range(1, 14)])
    assert v["overall"] == "ACCEPTED" and "p1_gate" not in v  # the progression gate is a separate layer
    assert v["pipeline_execution"]["status"] == "COMPLETED" and v["acceptance_criteria"]["status"] == "ACCEPTED"


def test_one_partial_criterion_makes_the_whole_report_incomplete_not_accepted():
    v = verdict([crit(PASS, cid="A1"), crit(PASS, DEFERRED, cid="A12")])
    assert v["overall"] == "INCOMPLETE"
    assert v["acceptance_criteria"]["not_passed"] == [{"id": "A12", "status": PARTIAL}]
    assert "A12 is PARTIAL" in v["acceptance_reasons"]


def test_a_failed_criterion_fails_the_report_even_if_everything_else_passed():
    v = verdict([crit(PASS, cid="A1"), crit(FAIL, PASS, cid="A2")])
    assert v["overall"] == "FAILED" and v["acceptance_criteria"]["status"] == "FAILED"


def test_a_genuine_stage_failure_blocks_acceptance_even_when_every_criterion_passes():
    failures = [{"stage": "prices", "symbol": "AMD", "kind": "NETWORK", "message": "down"}]
    v = verdict([crit(PASS, cid="A1")], failures=failures, succeeded=13)
    assert v["pipeline_execution"]["status"] == "COMPLETED_WITH_FAILURES" and v["overall"] == "FAILED"
    assert v["pipeline_execution"]["genuine_failures"] == failures


def test_an_expected_unsupported_issuer_is_listed_apart_from_failures_and_never_hides_an_unmet_criterion():
    passing = verdict([crit(PASS, cid="A1")], unsupported=UNSUPPORTED, succeeded=13)
    assert passing["pipeline_execution"]["status"] == "COMPLETED"  # not a failure
    assert passing["pipeline_execution"]["genuine_failures"] == []
    assert passing["pipeline_execution"]["expected_unsupported"][0]["symbol"] == "TSM"
    limits = {x["id"]: x for x in passing["known_coverage_limitations"]}
    assert "UNSUPPORTED_TAXONOMY:TSM" in limits and "MISSING_INPUT" in limits["UNSUPPORTED_TAXONOMY:TSM"]["text"]
    assert "NASDAQ_REVENUE_NOT_INGESTED" in limits  # the standing limitations are always stated
    assert verdict([crit(PASS, NOT_EVALUABLE, cid="A4")], unsupported=UNSUPPORTED)["overall"] == "INCOMPLETE"


def test_a_run_that_did_not_proceed_is_not_run_and_a_database_abort_is_a_failure():
    assert verdict([crit(NOT_EVALUABLE)], fatal="SEC_USER_AGENT_MISSING")["overall"] == "NOT_RUN"
    aborted = verdict([crit(PASS)], fatal="DATABASE_ERROR")
    assert aborted["pipeline_execution"]["status"] == "ABORTED" and aborted["overall"] == "FAILED"


def test_the_not_run_criteria_are_all_not_evaluable():
    from app.ingestion.p0_report import not_run_criteria

    criteria = not_run_criteria("because")
    assert len(criteria) == 13 and {c.status for c in criteria} == {NOT_EVALUABLE}
