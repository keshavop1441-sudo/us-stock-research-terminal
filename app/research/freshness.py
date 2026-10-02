"""Stale market data is never presented as current. Pure.

Prices and quotes are snapshots taken when they were ingested. Metrics that depend on them (price returns, drawdowns,
P/E, market cap and P/S) are only meaningful relative to the ``as_of`` date asked for. If the newest stored bar (or the
quote) is older than ``max_age_days`` before ``as_of``, those metrics become MISSING_INPUT with an explicit
``STALE_*`` reason instead of silently describing an older date. Fundamentals come from filings and are dated by their
own period/filing dates, so they are not affected. ``max_age_days=None`` disables blocking (the age is still reported).
"""

from datetime import date
from typing import Any

from app.screening.metrics import MetricResult, MetricState

DEFAULT_MAX_PRICE_AGE_DAYS = 7  # calendar days: a long weekend plus a holiday still passes
QUOTE_DEPENDENT_ISSUER_METRICS = ("issuer_market_cap", "price_to_sales", "price_to_sales_ttm")


def age_days(last: date | None, as_of: date) -> int | None:
    return None if last is None else (as_of - last).days


def is_stale(last: date | None, as_of: date, max_age_days: int | None) -> bool:
    if last is None:
        return True
    return max_age_days is not None and (as_of - last).days > max_age_days


def blocked(reason: str) -> MetricResult:
    return MetricResult(MetricState.MISSING_INPUT, None, reason)


def freshness_record(
    last_bar: date | None, quote_date: date | None, as_of: date, max_age_days: int | None
) -> dict[str, Any]:
    return {
        "as_of": as_of,
        "last_price_bar": last_bar,
        "price_age_days": age_days(last_bar, as_of),
        "quote_date": quote_date,
        "quote_age_days": age_days(quote_date, as_of),
        "max_age_days": max_age_days,
        "price_metrics_blocked": is_stale(last_bar, as_of, max_age_days) and max_age_days is not None,
        "quote_metrics_blocked": is_stale(quote_date, as_of, max_age_days) and max_age_days is not None,
    }


def apply_listing_freshness(
    metrics: dict[str, MetricResult],
    price_metric_names: set[str],
    last_bar: date | None,
    as_of: date,
    max_age: int | None,
) -> dict[str, MetricResult]:
    if max_age is None or not is_stale(last_bar, as_of, max_age):
        return metrics
    reason = "NO_PRICE_DATA" if last_bar is None else f"STALE_PRICE_DATA:last_bar={last_bar.isoformat()}"
    return {name: (blocked(reason) if name in price_metric_names else r) for name, r in metrics.items()}


def apply_issuer_freshness(
    metrics: dict[str, MetricResult], quote_date: date | None, as_of: date, max_age: int | None
) -> dict[str, MetricResult]:
    if max_age is None or not is_stale(quote_date, as_of, max_age):
        return metrics
    reason = "NO_QUOTE" if quote_date is None else f"STALE_QUOTE:quote_date={quote_date.isoformat()}"
    return {name: (blocked(reason) if name in QUOTE_DEPENDENT_ISSUER_METRICS else r) for name, r in metrics.items()}
