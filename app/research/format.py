"""Deterministic, human-readable rendering of metric values for evidence ``content`` strings. Pure.

The numbers come from the metric functions; this only formats them. A non-OK metric renders as ``N/A (STATE: reason)``,
never as 0 and never as a percentage.
"""

from app.research.catalog import CATALOG
from app.screening.metrics import MetricResult


def money(value: float) -> str:
    sign, v = ("-" if value < 0 else ""), abs(value)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= limit:
            return f"{sign}${v / limit:,.2f}{suffix}"
    return f"{sign}${v:,.2f}"


def render(name: str, result: MetricResult | None) -> str:
    if result is None:
        return "N/A (not produced)"
    if not result.ok:
        return f"N/A ({result.state.value}: {result.reason})" if result.reason else f"N/A ({result.state.value})"
    value = result.value
    assert value is not None
    unit = CATALOG[name].unit if name in CATALOG else "ratio"
    if unit == "fraction":
        return (
            f"{value * 100:+.1f}%"
            if "change" in name or "growth" in name or "return" in name or "drawdown" in name
            else f"{value * 100:.1f}%"
        )
    if unit == "usd":
        return money(value)
    if unit == "usd_per_share":
        return f"${value:,.2f}"
    if unit == "shares":
        return f"{value:,.0f}"
    suffix = "x" if name.startswith(("price_to", "debt_to")) else ""
    return f"{value:,.2f}{suffix}"
