"""SYNTHETIC SEC-shaped documents for hermetic tests. NOT provider data and never evidence of live behaviour.

The structure follows what the audit recorded of live documents (companyfacts point keys ``accn, end, filed, form, fp,
frame, fy, start, val``; submissions parallel arrays; ``company_tickers_exchange`` fields) but every number is
invented. Entity names say SYNTHETIC. Used by the fundamentals, normalisation and pipeline tests.
"""

import json
from datetime import date, timedelta

CIKS = {
    "AAPL": 320193, "NVDA": 1045810, "AMD": 2488, "GOOGL": 1652044, "GOOG": 1652044, "BRK-B": 1067983,
    "META": 1326801, "RIVN": 1874178, "PTON": 1639825, "KOSS": 56701, "COST": 909832, "JPM": 19617,
    "TSM": 1046179, "TSLA": 1318605,
}  # fmt: skip
EXCHANGES = {"AAPL": "Nasdaq", "GOOGL": "Nasdaq", "GOOG": "Nasdaq", "BRK-B": "NYSE", "TSM": "NYSE", "JPM": "NYSE"}


def ticker_map(symbols=tuple(CIKS)) -> dict:
    return {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[CIKS[s], f"SYNTHETIC {s}", s, EXCHANGES.get(s, "Nasdaq")] for s in symbols],
    }


class FactsBuilder:
    def __init__(self, cik: int, name: str = "SYNTHETIC TEST ISSUER"):
        self.cik, self.name = cik, name
        self.facts: dict[str, dict[str, dict]] = {}

    def add(
        self, tag, unit, start, end, val, accn, filed, form="10-K", fy=None, fp="FY", frame=None, taxonomy="us-gaap"
    ):
        point = {"end": str(end), "val": val, "accn": accn, "fy": fy, "fp": fp, "form": form, "filed": str(filed)}
        if start:
            point["start"] = str(start)
        if frame:
            point["frame"] = frame
        concept = self.facts.setdefault(taxonomy, {}).setdefault(tag, {"label": tag, "description": "", "units": {}})
        concept["units"].setdefault(unit, []).append(point)
        return self

    def build(self) -> dict:
        return {"cik": self.cik, "entityName": self.name, "facts": self.facts}


D = date


def standard_issuer(
    cik=320193,
    *,
    revenue_tag="RevenueFromContractWithCustomerExcludingAssessedTax",
    base_revenue=100e9,
    growth=1.10,
    net_margin=0.25,
    eps_base=5.0,
    split_in_q1=1,
    debt_lines=("short_term_debt", "current_portion_long_term_debt", "long_term_debt"),
    capex=True,
    gross_profit=True,
    q1=True,
    q1_prior=True,
    dei_shares=14.6e9,
    extra=None,
) -> dict:
    """Fiscal year ending 2025-09-27 (52/53-week style), 10-Ks for FY2024 and FY2025, optionally a Q1 FY2026 10-Q."""
    b = FactsBuilder(cik)
    ends = {2023: D(2023, 9, 30), 2024: D(2024, 9, 28), 2025: D(2025, 9, 27)}
    starts = {y: e - timedelta(days=363) for y, e in ends.items()}
    starts[2024] = D(2023, 10, 1)
    starts[2025] = D(2024, 9, 29)
    starts[2023] = D(2022, 10, 2)
    filings = {2024: ("0000000001-24-000100", D(2024, 11, 1)), 2025: ("0000000001-25-000100", D(2025, 10, 31))}
    revenue = {2023: base_revenue, 2024: base_revenue * growth, 2025: base_revenue * growth**2}

    def flows(year_filed):
        accn, filed = filings[year_filed]
        for fy_year in (year_filed, year_filed - 1, year_filed - 2):
            if fy_year not in ends:
                continue
            r = revenue[fy_year]
            for tag, unit, value in (
                (
                    (revenue_tag, "USD", r),
                    ("OperatingIncomeLoss", "USD", r * 0.3),
                    ("NetIncomeLoss", "USD", r * net_margin),
                    ("EarningsPerShareDiluted", "USD/shares", eps_base * r / base_revenue),
                    ("WeightedAverageNumberOfDilutedSharesOutstanding", "shares", 15e9),
                    ("NetCashProvidedByUsedInOperatingActivities", "USD", r * 0.28),
                )
                + ((("GrossProfit", "USD", r * 0.45),) if gross_profit else ())
                + ((("PaymentsToAcquirePropertyPlantAndEquipment", "USD", r * 0.04),) if capex else ())
            ):
                b.add(tag, unit, starts[fy_year], ends[fy_year], value, accn, filed, "10-K", year_filed, "FY")

    flows(2024)
    flows(2025)
    for year_filed in (2024, 2025):
        accn, filed = filings[year_filed]
        for fy_year in (year_filed, year_filed - 1):
            end = ends[fy_year]
            for tag, value in (
                ("CashAndCashEquivalentsAtCarryingValue", 30e9),
                ("StockholdersEquity", 60e9 + fy_year),
                ("ShortTermBorrowings", 5e9),
                ("LongTermDebtCurrent", 10e9),
                ("LongTermDebtNoncurrent", 90e9),
            ):
                line = {"ShortTermBorrowings": "short_term_debt",
                        "LongTermDebtCurrent": "current_portion_long_term_debt",
                        "LongTermDebtNoncurrent": "long_term_debt"}.get(tag)  # fmt: skip
                if line and line not in debt_lines:
                    continue
                b.add(tag, "USD", None, end, value, accn, filed, "10-K", year_filed, "FY")
    if dei_shares:
        b.add("EntityCommonStockSharesOutstanding", "shares", None, D(2025, 10, 17), dei_shares, filings[2025][0],
              filings[2025][1], "10-K", 2025, "FY", taxonomy="dei")  # fmt: skip
    if q1:
        accn, filed = "0000000001-26-000010", D(2026, 1, 30)
        qs, qe, ps, pe = D(2025, 9, 28), D(2025, 12, 27), D(2024, 9, 29), D(2024, 12, 28)
        qrev, prev = revenue[2025] * 1.05 / 4, revenue[2025] / 4
        eps_scale = split_in_q1
        for tag, unit, cur, prior in (
            (revenue_tag, "USD", qrev, prev),
            ("NetIncomeLoss", "USD", qrev * net_margin, prev * net_margin),
            ("EarningsPerShareDiluted", "USD/shares", eps_base * qrev / base_revenue / 4 * eps_scale * 4 / 4,
             eps_base * prev / base_revenue / 4),
            ("WeightedAverageNumberOfDilutedSharesOutstanding", "shares", 15e9 * split_in_q1, 15e9),
            ("NetCashProvidedByUsedInOperatingActivities", "USD", qrev * 0.28, prev * 0.28),
        ):  # fmt: skip
            b.add(tag, unit, qs, qe, cur, accn, filed, "10-Q", 2026, "Q1")
            b.add(tag, unit, ps, pe, prior, accn, filed, "10-Q", 2026, "Q1")
        b.add("CashAndCashEquivalentsAtCarryingValue", "USD", None, qe, 28e9, accn, filed, "10-Q", 2026, "Q1")
        b.add("StockholdersEquity", "USD", None, qe, 61e9, accn, filed, "10-Q", 2026, "Q1")
        for tag, line, value in (
            ("ShortTermBorrowings", "short_term_debt", 6e9),
            ("LongTermDebtCurrent", "current_portion_long_term_debt", 9e9),
            ("LongTermDebtNoncurrent", "long_term_debt", 88e9),
        ):
            if line in debt_lines:
                b.add(tag, "USD", None, qe, value, accn, filed, "10-Q", 2026, "Q1")
        if q1_prior:  # the 10-Q one year earlier: gives the prior-year TTM its own YTD comparison periods
            accn0, filed0 = "0000000001-25-000010", D(2025, 1, 31)
            for tag, unit, cur, prior in (
                (revenue_tag, "USD", prev, revenue[2024] / 4),
                ("NetIncomeLoss", "USD", prev * net_margin, revenue[2024] / 4 * net_margin),
            ):
                b.add(tag, unit, ps, pe, cur, accn0, filed0, "10-Q", 2025, "Q1")
                b.add(tag, unit, D(2023, 10, 1), D(2023, 12, 30), prior, accn0, filed0, "10-Q", 2025, "Q1")
    for tag, unit, start, end, val, accn, filed, form in extra or ():
        b.add(tag, unit, start, end, val, accn, filed, form, 2025, "FY")
    return b.build()


def submissions(
    cik: int, symbol: str, *, forms=(("10-K", "2025-10-31", "2025-09-27"), ("10-Q", "2026-01-30", "2025-12-27"))
):
    accession = [f"{cik:010d}-{25 + i}-0000{i}1" for i in range(len(forms))]
    # an optional 4th element is the 8-K item list ("2.02,9.01"); without it the ``items`` column is empty strings
    return {
        "cik": str(cik),
        "name": f"SYNTHETIC {symbol} INC",
        "sic": "3571",
        "sicDescription": "Electronic Computers",
        "tickers": [symbol],
        "exchanges": [EXCHANGES.get(symbol, "Nasdaq")],
        "fiscalYearEnd": "0926",
        "formerNames": [],
        "filings": {
            "recent": {
                "accessionNumber": accession,
                "form": [f[0] for f in forms],
                "filingDate": [f[1] for f in forms],
                "reportDate": [f[2] for f in forms],
                "items": [f[3] if len(f) > 3 else "" for f in forms],
                "primaryDocument": ["doc.htm"] * len(forms),
            }
        },
    }


def dumps(obj) -> bytes:
    return json.dumps(obj).encode()
