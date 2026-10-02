"""The P1 progression gate is a separate, deterministic decision layer: it never changes an acceptance status."""

import ast
import dataclasses
from pathlib import Path

import pytest

from app.ingestion import progression as pg
from app.ingestion.acceptance import DEFERRED, FAIL, NOT_EVALUABLE, PARTIAL, PASS, Component, Criterion
from app.ingestion.p0_report import REQUIRED_COMPONENTS

ROOT = Path(__file__).resolve().parent.parent
CLEAN = {"duplicate_price_keys": 0, "unnormalised_ciks": 0}
UNSUPPORTED = [{"symbol": "TSM", "cik": "0001046179", "kind": "UNSUPPORTED_TAXONOMY", "taxonomy": "ifrs-full"}]


def full_set(overrides=None):
    """All 13 criteria with every required component PASS, except the ones given as {(id, component): status}."""
    overrides = overrides or {}
    return [
        Criterion(cid, "m", "t", [Component(n, overrides.get((cid, n), PASS), "obs") for n in names])
        for cid, names in REQUIRED_COMPONENTS.items()
    ]


KNOWN_DEFERRED = {(p.criterion, p.component): (DEFERRED if p.live_trigger_required is False else NOT_EVALUABLE)
                  for p in pg.POLICIES}  # fmt: skip


def gate(criteria, **kw):
    args = dict(execution="COMPLETED", genuine_failures=[], expected_unsupported=UNSUPPORTED, mode="LIVE",
                integrity=CLEAN, automated_tests="PASSED")  # fmt: skip
    return pg.progression_gate(criteria, **{**args, **kw})


def test_the_latest_live_shape_opens_the_gate_while_every_criterion_keeps_its_honest_status():
    criteria = full_set(KNOWN_DEFERRED)
    result = gate(criteria)
    assert result["status"] == "OPEN" and result["blockers"] == []
    partial = {c.id for c in criteria if c.status == PARTIAL}
    assert partial == {"A3", "A4", "A7", "A10", "A11", "A12"}  # still PARTIAL: the gate redefines nothing
    assert {c["id"] for c in result["partial_criteria"]} == partial
    assert not any(c["blocks_p1"] for c in result["partial_criteria"])
    assert {(d["criterion"], d["component"]) for d in result["deferred_items"]} == set(pg.POLICY_INDEX)
    assert result["expected_unsupported"][0]["symbol"] == "TSM"


def test_every_policy_is_complete_names_a_real_required_component_and_is_not_applied_to_a_pass():
    for p in pg.POLICIES:
        assert p.component in REQUIRED_COMPONENTS[p.criterion], p
        assert p.reason and p.evidence_source
        assert p.blocking_for_p0_completion  # the strict acceptance statuses are untouched by the policy
    assert all(pg.POLICY_INDEX[k].deferred_to in {None, "P1", "P2", "FUTURE"} for k in pg.POLICY_INDEX)
    with pytest.raises(ValueError):
        dataclasses.replace(pg.POLICIES[0], deferred_to="SOMEDAY")
    with pytest.raises(ValueError):
        dataclasses.replace(pg.POLICIES[0], reason="")


def test_the_expected_policy_decisions_are_pinned():
    index = pg.POLICY_INDEX
    assert index[("A3", "split_rebase_true_detection")].live_trigger_required
    assert index[("A4", "total_assets")].deferred_to == "P1"
    assert index[("A7", "full_universe_projection")].deferred_to == "P1"
    assert index[("A10", "extrapolation_meets_a7")].deferred_to == "P2"
    assert index[("A12", "revenue_vs_nasdaq")].deferred_to == "FUTURE"
    assert not any(p.blocking_for_p1_entry for p in pg.POLICIES)


def test_a_non_pass_component_without_a_policy_fails_closed():
    result = gate(full_set({("A5", "core_fields_missing"): NOT_EVALUABLE, **KNOWN_DEFERRED}))
    assert result["status"] == "CLOSED"
    assert [b["source"] for b in result["blockers"]] == ["A5.core_fields_missing"]
    assert "no explicit progression policy" in result["blockers"][0]["reason"]
    assert next(c for c in result["partial_criteria"] if c["id"] == "A5")["blocks_p1"]


def test_a_policy_marked_blocking_closes_the_gate():
    policy = dataclasses.replace(pg.POLICY_INDEX[("A4", "total_assets")], blocking_for_p1_entry=True)
    result = gate(full_set(KNOWN_DEFERRED), policies={**pg.POLICY_INDEX, ("A4", "total_assets"): policy})
    assert result["status"] == "CLOSED" and result["blockers"][0]["source"] == "A4.total_assets"


def test_a_failed_component_always_blocks_even_if_it_has_a_non_blocking_policy():
    result = gate(full_set({**KNOWN_DEFERRED, ("A12", "revenue_vs_nasdaq"): FAIL}))
    assert result["status"] == "CLOSED" and result["blockers"][0]["source"] == "A12.revenue_vs_nasdaq"


@pytest.mark.parametrize(
    "kw,source",
    [
        ({"execution": "COMPLETED_WITH_FAILURES"}, "pipeline_execution"),
        ({"genuine_failures": [{"stage": "prices", "symbol": "AMD"}]}, "genuine_failures"),
        ({"mode": "SIMULATED"}, "live_run"),
        ({"integrity": {**CLEAN, "duplicate_fact_keys": 2}}, "data_integrity"),
        ({"integrity": None}, "data_integrity"),
        ({"integrity": {}}, "data_integrity"),
        ({"automated_tests": "FAILED"}, "automated_tests"),
        ({"automated_tests": None}, "automated_tests"),
    ],
)
def test_each_gate_condition_closes_the_gate_on_its_own(kw, source):
    result = gate(full_set(KNOWN_DEFERRED), **kw)
    assert result["status"] == "CLOSED" and [b["source"] for b in result["blockers"]] == [source]


def test_an_expected_unsupported_issuer_is_listed_but_is_not_a_blocker():
    result = gate(full_set(KNOWN_DEFERRED), expected_unsupported=UNSUPPORTED)
    assert result["status"] == "OPEN" and result["expected_unsupported"] == UNSUPPORTED


def test_the_gate_is_deterministic():
    criteria = full_set(KNOWN_DEFERRED)
    assert gate(criteria) == gate(criteria)


def test_a_fully_passing_run_with_no_deferrals_is_open_with_nothing_deferred():
    result = gate(full_set())
    assert result["status"] == "OPEN" and result["deferred_items"] == [] and result["partial_criteria"] == []


def test_criteria_round_trip_through_a_stored_report():
    criteria = full_set(KNOWN_DEFERRED)
    rebuilt = pg.criteria_from_report([c.to_dict() for c in criteria])
    assert [(c.id, c.status) for c in rebuilt] == [(c.id, c.status) for c in criteria]
    assert gate(rebuilt) == gate(criteria)


def test_every_evidence_test_named_by_a_policy_exists():
    for p in pg.POLICIES:
        for ref in p.evidence_source:
            path, _, name = ref.partition("::")
            path = path.partition("#")[0]
            assert (ROOT / path).exists(), ref
            if name:
                functions = {n.name for n in ast.walk(ast.parse((ROOT / path).read_text(encoding="utf-8")))
                             if isinstance(n, ast.FunctionDef)}  # fmt: skip
                assert name in functions, ref
