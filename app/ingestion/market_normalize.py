"""Provider rows -> validated ``PriceRecord`` / ``MarketQuoteRecord`` / classification. Pure functions.

Price semantics (docs/data_coverage.yaml ``price_semantics``): ``close`` is split-adjusted and NOT dividend-adjusted;
``adj_close`` is always NULL; nothing here produces or implies a total return. A row that is not a plausible bar is
REJECTED and reported, never repaired. Missing trading days are not invented: gaps are measured and reported
(``gaps``), not filled.
"""

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from app.models.records import MarketQuoteRecord, PriceRecord

GAP_REPORT_THRESHOLD_BUSINESS_DAYS = 5  # YAML A3: every unexplained gap over 5 trading days is reviewed
SPLIT_RATIO_TOLERANCE = 0.001
# Named tuple, not `except TypeError, ValueError:` (3.14-only syntax; see app/database/locking.py).
_NOT_A_NUMBER = (TypeError, ValueError)


def _num(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except _NOT_A_NUMBER:
        return None
    return number if math.isfinite(number) else None


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


@dataclass
class PriceParse:
    records: list[PriceRecord] = field(default_factory=list)
    rejects: list[dict[str, object]] = field(default_factory=list)
    duplicates: int = 0  # the provider repeated a trade date (identical or not): last one wins, counted
    # one entry per repeated date: {"date": iso, "identical": bool, "kept_close": x, "dropped_close": y}
    duplicate_details: list[dict[str, object]] = field(default_factory=list)
    gaps: list[tuple[date, date, int]] = field(default_factory=list)  # (last bar, next bar, business days between)
    first: date | None = None
    last: date | None = None


def business_days_between(a: date, b: date) -> int:
    """Weekdays strictly between two dates (holidays are NOT removed: a long gap is reported for review, not judged)."""
    days = (b - a).days - 1
    return sum(1 for i in range(1, days + 1) if (a.toordinal() + i - 1) % 7 < 5) if days > 0 else 0


def parse_prices(security_id: int, rows: list[dict], source_id: int | None = None) -> PriceParse:
    result = PriceParse()
    by_date: dict[date, PriceRecord] = {}
    for row in rows:
        day = _as_date(row.get("date"))
        o, h, lo, c = (_num(row.get(k)) for k in ("open", "high", "low", "close"))
        volume = _num(row.get("volume"))
        reason = None
        if day is None:
            reason = "BAD_DATE"
        elif c is None or c <= 0:
            reason = "NO_POSITIVE_CLOSE"
        elif any(v is not None and v <= 0 for v in (o, h, lo)):
            reason = "NON_POSITIVE_PRICE"
        elif h is not None and lo is not None and h < lo:
            reason = "HIGH_BELOW_LOW"
        elif h is not None and c > h * 1.0001 or lo is not None and c < lo * 0.9999:
            reason = "CLOSE_OUTSIDE_RANGE"
        elif volume is not None and volume < 0:
            reason = "NEGATIVE_VOLUME"
        if reason:
            result.rejects.append({"row": {k: str(v) for k, v in row.items()}, "reason": reason})
            continue
        if day in by_date:
            result.duplicates += 1
            previous = by_date[day]
            result.duplicate_details.append(
                {
                    "date": day.isoformat(),
                    "identical": (previous.open, previous.high, previous.low, previous.close, previous.volume)
                    == (o, h, lo, c, int(volume) if volume is not None else None),
                    "kept_close": c,
                    "dropped_close": previous.close,
                }
            )
        by_date[day] = PriceRecord(
            security_id=security_id,
            trade_date=day,
            open=o,
            high=h,
            low=lo,
            close=c,
            adj_close=None,  # no provider returns a dividend-adjusted close (price_semantics)
            volume=int(volume) if volume is not None else None,
            source_id=source_id,
        )
    result.records = [by_date[d] for d in sorted(by_date)]
    dates = [r.trade_date for r in result.records]
    if dates:
        result.first, result.last = dates[0], dates[-1]
    for earlier, later in zip(dates, dates[1:], strict=False):
        gap = business_days_between(earlier, later)
        if gap > GAP_REPORT_THRESHOLD_BUSINESS_DAYS:
            result.gaps.append((earlier, later, gap))
    return result


def detect_rebase(stored: dict[date, float], new: list[PriceRecord]) -> float | None:
    """Ratio new/stored closes over overlapping dates when it is not 1 (a split re-based the provider history), else
    None.

    Phase 2: 'a refresh must compare overlapping dates and reload the full history when the ratio differs from 1'. The
    full window is always re-upserted here, so the detection is for REPORTING (the changed rows are counted as updates).
    """
    ratios = sorted(r.close / stored[r.trade_date] for r in new if r.close and stored.get(r.trade_date))
    if len(ratios) < 5:
        return None
    median = ratios[len(ratios) // 2]
    return median if abs(median - 1) > SPLIT_RATIO_TOLERANCE else None


@dataclass(frozen=True)
class QuoteParse:
    record: MarketQuoteRecord
    sector: str | None
    industry: str | None
    exchange: str | None


def parse_quote(security_id: int, row: dict, source_id: int | None = None) -> QuoteParse:
    """Nasdaq quote row -> record. The quote date is the provider's own ``last_timestamp`` date (never the retrieval
    date)."""
    quote_date = _as_date(row.get("last_timestamp"))
    if quote_date is None:
        raise ValueError("quote has no usable last_timestamp: cannot place it in time")

    def text(key: str) -> str | None:
        value = row.get(key)
        return value.strip() if isinstance(value, str) and value.strip() and value.strip().upper() != "N/A" else None

    return QuoteParse(
        MarketQuoteRecord(
            security_id=security_id,
            quote_date=quote_date,
            last_price=_num(row.get("last_price")),
            market_cap=_num(row.get("market_cap")),
            year_high=_num(row.get("year_high")),
            year_low=_num(row.get("year_low")),
            source_id=source_id,
        ),
        text("sector"),
        text("industry"),
        text("exchange"),
    )


def count_rejects(rejects: list[dict[str, object]]) -> Counter:
    return Counter(str(r["reason"]) for r in rejects)
