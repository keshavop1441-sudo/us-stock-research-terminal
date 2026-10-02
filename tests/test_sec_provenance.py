"""Raw SEC companyfacts are the provenance layer: every field of a point survives parsing and reaches the database.

Raw point key sets are those observed LIVE (probe run 36954761165, raw_facts / dei_shares): duration facts carry
accn, end, filed, form, fp, frame, fy, start, val; instant facts (dei shares) have no ``start``. Values come from the
live RIVN / AAPL fixtures; the accession numbers and frames below are constructed for the test and marked as such.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from app.database import access
from app.models.records import FinancialFactRecord
from app.models.sec_facts import RAW_POINT_KEYS, RawFactPoint, parse_companyfacts_point

LIVE = Path(__file__).resolve().parent / "fixtures" / "live_probe_2026_10_02"
RIVN = json.loads((LIVE / "rivn_net_income.json").read_text(encoding="utf-8"))["raw_companyfacts_fy"]
AAPL_DEI = json.loads((LIVE / "dei_shares.json").read_text(encoding="utf-8"))["AAPL"]["latest"]

# constructed accession/frame values (the live log lines kept only end/val/form/filed for these points)
FY2025 = {
    "accn": "0001874178-26-000008", "start": "2025-01-01", "end": RIVN["NetIncomeLoss"][-1][0],
    "val": RIVN["NetIncomeLoss"][-1][1], "form": "10-K", "filed": RIVN["NetIncomeLoss"][-1][3],
    "fy": 2025, "fp": "FY", "frame": "CY2025",
}  # fmt: skip


def parse(point, concept="NetIncomeLoss", unit="USD", taxonomy="us-gaap", cik=1874178):
    return parse_companyfacts_point(cik, taxonomy, concept, unit, point)


def test_the_documented_point_keys_are_exactly_the_ones_observed_live():
    assert {"accn", "end", "filed", "form", "fp", "frame", "fy", "start", "val"} == RAW_POINT_KEYS


def test_every_provenance_field_survives_parsing():
    raw = parse(FY2025)
    assert (raw.cik, raw.taxonomy, raw.concept, raw.unit) == ("0001874178", "us-gaap", "NetIncomeLoss", "USD")
    assert raw.value == -3646000000.0 and raw.period_start == date(2025, 1, 1) and raw.period_end == date(2025, 12, 31)
    assert raw.filed == date(2026, 2, 12) and raw.form == "10-K"
    assert (raw.fiscal_year, raw.fiscal_period, raw.frame, raw.accession) == (
        2025,
        "FY",
        "CY2025",
        "0001874178-26-000008",
    )
    assert not raw.is_instant


def test_instant_facts_have_no_start_and_stay_instants():
    _end, value, form, filed = AAPL_DEI[-1]
    point = {
        "accn": "0000320193-26-000099",
        "end": _end,
        "val": value,
        "form": form,
        "filed": filed,
        "fy": 2026,
        "fp": "Q3",
    }
    raw = parse(point, "EntityCommonStockSharesOutstanding", "shares", "dei", 320193)
    assert raw.is_instant and raw.period_start is None and raw.value == 14594180000.0 and raw.frame is None


def test_deterministic_source_references():
    raw = parse(FY2025)
    assert raw.source_ref == "https://data.sec.gov/api/xbrl/companyfacts/CIK0001874178.json#us-gaap/NetIncomeLoss/USD"
    assert raw.filing_url == "https://www.sec.gov/Archives/edgar/data/1874178/000187417826000008/"
    assert parse({**FY2025, "accn": None}).filing_url is None  # no accession -> no invented URL


def test_schema_drift_and_malformed_points_are_rejected_not_ignored():
    with pytest.raises(ValueError, match="unexpected companyfacts keys"):
        parse({**FY2025, "new_sec_field": 1})
    with pytest.raises(ValueError, match="lacks required keys"):
        parse({"end": "2025-12-31", "val": 1})
    with pytest.raises(ValueError):
        parse({**FY2025, "end": "not-a-date"})


def test_conversion_to_the_write_record_keeps_everything_the_schema_can_hold():
    record = parse(FY2025).to_record(source_id=7)
    assert isinstance(record, FinancialFactRecord)
    assert (record.taxonomy, record.concept, record.unit, record.value) == (
        "us-gaap",
        "NetIncomeLoss",
        "USD",
        -3646000000.0,
    )
    assert (record.period_start, record.period_end, record.filed_date) == (
        date(2025, 1, 1),
        date(2025, 12, 31),
        date(2026, 2, 12),
    )
    assert (record.form, record.fiscal_year, record.fiscal_period, record.accession_no, record.source_id) == (
        "10-K", 2025, "FY", "0001874178-26-000008", 7,
    )  # fmt: skip
    assert "frame" not in FinancialFactRecord.model_fields  # schema gap G7: kept on RawFactPoint until a column exists


def test_restated_vintages_reach_the_database_as_separate_rows_with_their_provenance(db_path, con):
    first = parse(
        {
            **FY2025,
            "accn": "0001874178-25-000010",
            "filed": "2025-02-24",
            "end": "2024-12-31",
            "start": "2024-01-01",
            "fy": 2024,
        }
    )
    again = parse(
        {**FY2025, "accn": "0001874178-26-000008", "end": "2024-12-31", "start": "2024-01-01", "fy": 2024}
    )  # re-presented later
    with access.writer(db_path, "test") as w:
        w.upsert_financial_facts([first.to_record(), again.to_record()])
        w.upsert_financial_facts([first.to_record(), again.to_record()])
    rows = con.execute(
        "SELECT accession_no, filed_date, form, fiscal_year, fiscal_period FROM financial_facts ORDER BY filed_date"
    ).fetchall()
    assert rows == [
        ("0001874178-25-000010", date(2025, 2, 24), "10-K", 2024, "FY"),
        ("0001874178-26-000008", date(2026, 2, 12), "10-K", 2024, "FY"),
    ]


def test_raw_point_is_frozen_and_validated():
    raw = parse(FY2025)
    with pytest.raises(ValueError):
        raw.value = 1.0  # type: ignore[misc]
    assert isinstance(raw, RawFactPoint)
