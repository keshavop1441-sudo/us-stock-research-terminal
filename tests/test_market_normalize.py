"""Provider price/quote rows -> records (SYNTHETIC rows): validation, duplicates, gaps, split re-base, price
semantics."""

from datetime import date

import pytest

from app.ingestion.market_normalize import business_days_between, detect_rebase, parse_prices, parse_quote


def bar(day, close=10.0, **kw):
    return {"date": day, "open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1000, **kw}


def test_valid_rows_become_records_with_split_adjusted_close_and_no_adjusted_close():
    parsed = parse_prices(7, [bar("2026-01-05"), bar("2026-01-02", 9.0)], source_id=3)
    assert [r.trade_date for r in parsed.records] == [date(2026, 1, 2), date(2026, 1, 5)]  # sorted
    assert all(r.adj_close is None and r.security_id == 7 and r.source_id == 3 for r in parsed.records)
    assert (parsed.first, parsed.last) == (date(2026, 1, 2), date(2026, 1, 5))


def test_duplicate_trade_dates_are_collapsed_last_wins_and_counted():
    parsed = parse_prices(1, [bar("2026-01-02", 1.0), bar("2026-01-02", 2.0)])
    assert len(parsed.records) == 1 and parsed.records[0].close == 2.0 and parsed.duplicates == 1


@pytest.mark.parametrize(
    "row,reason",
    [
        (bar("not-a-date"), "BAD_DATE"),
        ({**bar("2026-01-02"), "close": None}, "NO_POSITIVE_CLOSE"),
        ({**bar("2026-01-02"), "close": float("nan")}, "NO_POSITIVE_CLOSE"),
        ({**bar("2026-01-02"), "close": -1}, "NO_POSITIVE_CLOSE"),
        ({**bar("2026-01-02"), "high": 5, "low": 6, "close": 5.5}, "HIGH_BELOW_LOW"),
        ({**bar("2026-01-02"), "close": 20.0}, "CLOSE_OUTSIDE_RANGE"),
        ({**bar("2026-01-02"), "volume": -5}, "NEGATIVE_VOLUME"),
    ],
)
def test_implausible_rows_are_rejected_and_reported_never_repaired(row, reason):
    parsed = parse_prices(1, [row, bar("2026-01-05")])
    assert [r["reason"] for r in parsed.rejects] == [reason] and len(parsed.records) == 1


def test_missing_trading_days_are_reported_as_gaps_never_filled():
    rows = [bar("2026-01-02"), bar("2026-01-05"), bar("2026-01-30")]  # a 3-week hole
    parsed = parse_prices(1, rows)
    assert len(parsed.records) == 3  # nothing invented
    assert parsed.gaps == [(date(2026, 1, 5), date(2026, 1, 30), 18)]
    assert business_days_between(date(2026, 1, 2), date(2026, 1, 5)) == 0  # a weekend is not a gap
    assert parse_prices(1, [bar("2026-01-02"), bar("2026-01-09")]).gaps == []  # 4 missing business days <= threshold


def test_a_split_that_re_bases_history_is_detected_from_overlapping_closes():
    rows = [bar(f"2026-01-{d:02d}", 5.0) for d in range(5, 12)]  # provider now reports half the old price
    new = parse_prices(1, rows).records
    stored = {r.trade_date: 10.0 for r in new}
    assert detect_rebase(stored, new) == pytest.approx(0.5)
    assert detect_rebase({r.trade_date: 5.0 for r in new}, new) is None
    assert detect_rebase({}, new) is None  # first load: nothing to compare


def test_quote_uses_the_providers_date_and_keeps_raw_classification_text():
    row = {"last_timestamp": date(2026, 10, 1), "last_price": 330.0, "market_cap": 4.8e12, "year_high": 345.3,
           "year_low": 243.4,
           "sector": "Technology", "industry": "N/A", "exchange": "NASDAQ-GS"}  # fmt: skip
    parsed = parse_quote(4, row, source_id=9)
    assert (
        parsed.record.quote_date == date(2026, 10, 1)
        and parsed.record.market_cap == 4.8e12
        and parsed.record.source_id == 9
    )
    assert (parsed.sector, parsed.industry) == ("Technology", None)  # 'N/A' is unknown, not a value
    with pytest.raises(ValueError, match="last_timestamp"):
        parse_quote(4, {**row, "last_timestamp": None})


def test_non_finite_quote_numbers_become_none():
    parsed = parse_quote(1, {"last_timestamp": "2026-10-01", "market_cap": float("inf"), "last_price": "n/a"})
    assert parsed.record.market_cap is None and parsed.record.last_price is None


def test_duplicate_details_say_whether_the_repeated_rows_were_identical_or_conflicting():
    same = parse_prices(1, [bar("2026-01-02", 5.0), bar("2026-01-02", 5.0)])
    assert same.duplicates == 1 and same.duplicate_details == [
        {"date": "2026-01-02", "identical": True, "kept_close": 5.0, "dropped_close": 5.0}
    ]
    differ = parse_prices(1, [bar("2026-01-02", 5.0), bar("2026-01-02", 6.0), bar("2026-01-05", 7.0)])
    assert differ.duplicates == 1 and differ.records[0].close == 6.0  # last wins, unchanged behaviour
    assert differ.duplicate_details[0]["identical"] is False and differ.duplicate_details[0]["dropped_close"] == 5.0
