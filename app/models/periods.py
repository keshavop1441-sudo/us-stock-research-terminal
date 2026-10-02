"""Parsers for the date/period spellings the audited providers use (all verified in the 2026-10-02 probes).

SEC submissions ``fiscalYearEnd`` is ``MMDD`` ("0926"); Nasdaq earnings ``fiscal_period_ending`` is "Jun 2026" and its
calendar ``period_ending`` is "2026-06"; SEC/Cboe/Nasdaq price dates are ISO "YYYY-MM-DD" strings. Fiscal years are
52/53-week for some filers (AAPL's year ends on the last Saturday of September), so ``fiscalYearEnd`` is the NOMINAL
month/day only; actual period ends come from the filings (``period_end``), never from this value.
"""

import calendar
import re
from datetime import date

_MONTHS = {name.lower(): number for number, name in enumerate(calendar.month_abbr) if name}


def parse_fiscal_year_end(value: str | None) -> tuple[int, int] | None:
    """'0926' -> (9, 26). None/empty -> None (unknown). Anything else malformed raises ValueError."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    if not re.fullmatch(r"\d{4}", text):
        raise ValueError(f"fiscalYearEnd must be MMDD, got {value!r}")
    month, day = int(text[:2]), int(text[2:])
    if not 1 <= month <= 12 or not 1 <= day <= calendar.monthrange(2024, month)[1]:  # 2024: leap year, 0229 is legal
        raise ValueError(f"fiscalYearEnd is not a calendar date: {value!r}")
    return month, day


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def parse_month_year(value: str) -> date:
    """'Jun 2026' (Nasdaq fiscal_period_ending) or '2026-06' (Nasdaq calendar period_ending) -> 2026-06-30.

    Month granularity only: the day is the month end, an approximation of the real period end that must not be used as a
    key in place of the filing's own period end.
    """
    text = value.strip()
    iso = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if iso:
        return month_end(int(iso[1]), int(iso[2]))
    named = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{4})", text)
    if named and named[1].lower() in _MONTHS:
        return month_end(int(named[2]), _MONTHS[named[1].lower()])
    raise ValueError(f"unrecognised month/year spelling: {value!r}")


def parse_iso_date(value: str) -> date:
    """Strict 'YYYY-MM-DD' (also accepts the date part of an ISO timestamp, e.g. '2026-10-01T00:00:00')."""
    return date.fromisoformat(value.strip()[:10])
