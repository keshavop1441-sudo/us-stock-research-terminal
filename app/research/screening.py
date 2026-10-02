"""Deterministic screen evaluation. Pure: no I/O, no database, no provider calls.

Input: a validated ``ScreenSpec`` and one ``SecurityView`` per stored listing (metrics already computed by
``app.screening``). Output: per security, the exact metric value, the threshold used, a PASS / FAIL / UNEVALUABLE
verdict per criterion and an overall status:

    MATCH        every criterion PASSED
    NO_MATCH     at least one criterion FAILED (its value is known and does not satisfy the condition)
    UNEVALUABLE  nothing failed, but at least one criterion could not be evaluated (missing / not comparable / not
                 meaningful input). These are reported as MISSING-DATA EXCLUSIONS, never as matches and never as
                 zeros.

There is no score and no ranking: results are listed alphabetically by ticker. A non-OK metric is never turned into 0,
a percentage or an infinity (docs/data_coverage.yaml ``comparability_rules``).
"""

from dataclasses import dataclass, field
from typing import Any

from app.research.catalog import CATALOG
from app.research.envelope import metric_dict
from app.research.screen_spec import OP_SYMBOL, ClassificationFilter, Criterion, ScreenSpec
from app.screening import classification as cls
from app.screening.metrics import MetricResult

PASS, FAIL, UNEVALUABLE = "PASS", "FAIL", "UNEVALUABLE"
MATCH, NO_MATCH = "MATCH", "NO_MATCH"
_EQ_TOLERANCE = 1e-9


@dataclass(frozen=True)
class SecurityView:
    ticker: str
    cik: str
    name: str | None
    issuer: dict[str, MetricResult]
    listing: dict[str, MetricResult]
    nasdaq: cls.Classification | None = None
    sic: cls.Classification | None = None
    provenance: dict[str, Any] = field(default_factory=dict)  # metric name -> refs, filled by the service layer


def _compare(op: str, value: float, threshold: tuple[float | None, float | None, float | None]) -> bool:
    target, low, high = threshold
    if op == "between":
        return low <= value <= high  # type: ignore[operator]
    assert target is not None
    return {
        "gt": value > target,
        "gte": value >= target,
        "lt": value < target,
        "lte": value <= target,
        "eq": abs(value - target) <= _EQ_TOLERANCE * max(1.0, abs(target)),
    }[op]


def _describe(criterion: Criterion) -> str:
    value, low, high = criterion.scaled()
    if criterion.op == "between":
        return f"{criterion.metric} between {low} and {high} (inclusive)"
    return f"{criterion.metric} {OP_SYMBOL[criterion.op]} {value}"


def evaluate_criterion(criterion: Criterion, view: SecurityView) -> dict[str, Any]:
    spec = CATALOG[criterion.metric]
    results = view.issuer if spec.scope == "issuer" else view.listing
    result = results.get(criterion.metric)
    value, low, high = criterion.scaled()
    out: dict[str, Any] = {
        "metric": criterion.metric,
        "scope": spec.scope,
        "condition": _describe(criterion),
        "op": criterion.op,
        "threshold": {"value": value, "low": low, "high": high, "unit": spec.unit},
        "threshold_as_entered": {
            "value": criterion.value,
            "low": criterion.low,
            "high": criterion.high,
            "unit": criterion.unit,
        },
        "provenance": view.provenance.get(criterion.metric, []),
    }
    if result is None:  # the snapshot did not produce this metric at all: unknown, never zero
        out.update(result=UNEVALUABLE, metric_state="MISSING_INPUT", value=None, reason="METRIC_NOT_PRODUCED", flags=[])
        return out
    out.update(metric_state=result.state.value, value=result.value, reason=result.reason, flags=list(result.flags))
    if not result.ok:
        out["result"] = UNEVALUABLE
    else:
        out["result"] = PASS if _compare(criterion.op, result.value, (value, low, high)) else FAIL  # type: ignore[arg-type]
    return out


def evaluate_classification(flt: ClassificationFilter, view: SecurityView) -> dict[str, Any]:
    """One taxonomy only. Missing classification is UNEVALUABLE (unknown), never a failure and never a match."""
    if flt.taxonomy == "nasdaq":
        c = view.nasdaq
        verdict = cls.matches_nasdaq(
            c, sector=flt.sector, industries=flt.industries, exclude_industries=flt.exclude_industries
        )
        values = {"sector": c.sector if c else None, "industry": c.industry if c else None}
        source = "nasdaq (raw Nasdaq labels)"
    else:
        c = view.sic
        verdict = cls.matches_sic(c, codes=flt.sic_codes)
        values = {"sic": c.code if c else None, "sic_description": c.industry if c else None}
        source = "sec (SIC code)"
    return {
        "metric": "classification",
        "taxonomy": flt.taxonomy,
        "condition": flt.model_dump(exclude_defaults=True),
        "values": values,
        "source": source,
        "result": UNEVALUABLE if verdict is None else (PASS if verdict else FAIL),
        "reason": "CLASSIFICATION_NOT_AVAILABLE" if verdict is None else None,
    }


def evaluate_security(spec: ScreenSpec, view: SecurityView) -> dict[str, Any]:
    outcomes: list[dict[str, Any]] = []
    if spec.classification is not None:
        outcomes.append(evaluate_classification(spec.classification, view))
    outcomes.extend(evaluate_criterion(c, view) for c in spec.criteria)
    failed = [o for o in outcomes if o["result"] == FAIL]
    unknown = [o for o in outcomes if o["result"] == UNEVALUABLE]
    if failed:
        status = NO_MATCH
        reason = "FAILED: " + "; ".join(_failure_text(o) for o in failed)
    elif unknown:
        status = UNEVALUABLE
        reason = "MISSING_DATA: " + "; ".join(_unknown_text(o) for o in unknown)
    else:
        status, reason = MATCH, None
    return {
        "ticker": view.ticker,
        "cik": view.cik,
        "name": view.name,
        "status": status,
        "exclusion_reason": reason,
        "criteria": outcomes,
        "criteria_passed": sum(o["result"] == PASS for o in outcomes),
        "criteria_failed": len(failed),
        "criteria_unevaluable": len(unknown),
    }


def _failure_text(o: dict[str, Any]) -> str:
    if o["metric"] == "classification":
        return f"classification {o['values']} does not satisfy the {o['taxonomy']} filter"
    return f"{o['condition']} is false (value {o['value']})"


def _unknown_text(o: dict[str, Any]) -> str:
    if o["metric"] == "classification":
        return f"classification unavailable in taxonomy {o['taxonomy']}"
    return f"{o['metric']} {o['metric_state']}" + (f" ({o['reason']})" if o.get("reason") else "")


def run_screen(spec: ScreenSpec, views: list[SecurityView]) -> dict[str, Any]:
    """Evaluate every view. Alphabetical, no score, no ranking."""
    evaluated = [evaluate_security(spec, v) for v in sorted(views, key=lambda v: (v.ticker, v.cik))]
    by_status = {s: [e for e in evaluated if e["status"] == s] for s in (MATCH, NO_MATCH, UNEVALUABLE)}
    return {
        "matches": by_status[MATCH],
        "failed": by_status[NO_MATCH],
        "missing_data_exclusions": by_status[UNEVALUABLE],
        "counts": {
            "screened": len(evaluated),
            "matches": len(by_status[MATCH]),
            "failed": len(by_status[NO_MATCH]),
            "missing_data_exclusions": len(by_status[UNEVALUABLE]),
        },
        "ordering": "alphabetical by ticker; no score and no ranking is produced",
    }


def metric_row(results: dict[str, MetricResult]) -> dict[str, Any]:
    return {name: metric_dict(r) for name, r in results.items()}
