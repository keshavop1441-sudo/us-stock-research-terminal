"""The machine-readable result every research command returns: ONE JSON object (the envelope).

Claude consumes this, never human-formatted text. The envelope is deliberately small and stable:

    {"schema_version": "1.0", "command": "...", "status": "OK" | "PARTIAL" | "ERROR",
     "generated_at": "<UTC ISO timestamp>", "data": {...}, "warnings": [...], "errors": [...]}

``status`` OK    = everything requested was produced;
          PARTIAL = something requested is missing/unavailable (see ``warnings`` and the per-item states in ``data``);
          ERROR   = nothing usable was produced (see ``errors``; ``data`` may still carry diagnostics).
Missing values are always explicit (``null`` plus a state/reason), never 0 and never omitted silently.
"""

import math
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import PurePath
from typing import Any

from pydantic import BaseModel

from app.screening.metrics import MetricResult

SCHEMA_VERSION = "1.0"
STATUS_OK, STATUS_PARTIAL, STATUS_ERROR = "OK", "PARTIAL", "ERROR"


def metric_dict(result: MetricResult) -> dict[str, Any]:
    """A metric as it appears in every output: value is null unless state is OK."""
    return {"state": result.state.value, "value": result.value, "reason": result.reason, "flags": list(result.flags)}


def to_jsonable(obj: Any) -> Any:
    """Recursively convert to plain JSON types. NaN/inf become null (JSON has no representation for them)."""
    if obj is None or isinstance(obj, bool | int | str):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, datetime):
        return obj.replace(microsecond=0).isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, MetricResult):
        return metric_dict(obj)
    if isinstance(obj, BaseModel):
        return to_jsonable(obj.model_dump())
    if is_dataclass(obj) and not isinstance(obj, type):
        return to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set | frozenset):
        items = sorted(obj, key=str) if isinstance(obj, set | frozenset) else obj
        return [to_jsonable(v) for v in items]
    if isinstance(obj, PurePath):
        return obj.name  # never leak a local absolute path into an output
    raise TypeError(f"not JSON-serialisable: {type(obj).__name__}")


def make_envelope(
    command: str,
    data: dict[str, Any] | None = None,
    *,
    status: str = STATUS_OK,
    warnings: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if status not in (STATUS_OK, STATUS_PARTIAL, STATUS_ERROR):
        raise ValueError(f"unknown status {status!r}")
    stamp = (now or datetime.now(UTC)).replace(tzinfo=None, microsecond=0)
    return to_jsonable(
        {
            "schema_version": SCHEMA_VERSION,
            "command": command,
            "status": status,
            "generated_at": stamp,
            "data": data or {},
            "warnings": warnings or [],
            "errors": errors or [],
        }
    )


def problem(code: str, message: str, **details: Any) -> dict[str, Any]:
    """A warning/error entry: a stable machine-readable ``code`` plus a human sentence."""
    return {"code": code, "message": message, **({"details": details} if details else {})}
