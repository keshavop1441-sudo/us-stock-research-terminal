"""The P0 representative set: fixed, documented and pinned by tests (``tests/test_p0_manifest.py``).

Selection method: hand-chosen from the Phase 2 probe evidence (``docs/data_coverage.yaml`` ``representative_companies``,
``sample_tested`` lists) so that every Phase 2 edge case the pilot must prove is covered by a name whose behaviour was
observed live on 2026-10-02. This deliberately differs from the P1 rule (seeded stratified draw): P0 is a dry run on the
audit's known cases (YAML ``universe_pilot.stages[P0_dry_run]``), widened from "5-10" to 14 listings / 13 issuers
because the brief asks for ten-plus coverage categories. The set is never edited during a run; changing it changes
``manifest_digest()`` and fails the pinning test, so a silent substitution is impossible.

``phase2_cik`` is the CIK the audit recorded for the symbol (None when the audit recorded the symbol but not its CIK).
The pipeline resolves the CIK from SEC data and treats a disagreement with ``phase2_cik`` as a failure, never as
something to overwrite.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date

from app.models.symbols import canonical_symbol

# Price history window: fixed (not "today minus N") so a re-run requests the same window. 2020 start gives the
# 5-year return a margin and covers the 2020 AAPL and 2022 AMZN-style split re-basing the audit measured.
PRICE_HISTORY_START = date(2020, 1, 1)


@dataclass(frozen=True)
class P0Security:
    symbol: str  # canonical (SEC) spelling
    issuer: str  # short issuer label
    roles: tuple[str, ...]  # coverage categories this name proves
    why: str
    phase2_cik: str | None = None
    primary_listing: bool = True  # the designated primary listing used for issuer_market_cap


def _s(symbol: str, issuer: str, roles: tuple[str, ...], why: str, cik: int | None = None, primary: bool = True):
    return P0Security(canonical_symbol(symbol), issuer, roles, why, f"{cik:010d}" if cik else None, primary)


ROLES = (
    "large_tech", "semiconductor", "multi_class", "loss_making", "non_calendar_fy", "financial",
    "smaller_cap", "adr", "ticker_spelling", "edge_reporting", "amendment",
)  # fmt: skip

MANIFEST: tuple[P0Security, ...] = (
    _s("AAPL", "Apple", ("large_tech", "non_calendar_fy"),
       "FY ends late September (52/53 weeks), single class, TTM share-sum trap; baseline for every loader.", 320193),
    _s("NVDA", "NVIDIA", ("semiconductor", "non_calendar_fy", "edge_reporting"),
       "January FY; 10-for-1 split June 2024: per-share vintage trap (FY2024 EPS 1.19 vs 11.93).", 1045810),
    _s("AMD", "Advanced Micro Devices", ("semiconductor", "non_calendar_fy", "amendment"),
       "10-K/A filed 2026-02-04 (accession 0000002488-26-000021): amendment handling; FY ends late December.", 2488),
    _s("GOOGL", "Alphabet", ("multi_class", "large_tech"),
       "Primary listing of the issuer; GOOG/GOOGL/GOOGM/GOOGN share CIK 0001652044; no dei shares concept.", 1652044),
    _s("GOOG", "Alphabet", ("multi_class",),
       "Second listing of the same issuer: must resolve to the same CIK and issuer_market_cap.", 1652044, False),
    _s("BRK-B", "Berkshire Hathaway", ("multi_class", "ticker_spelling", "financial", "edge_reporting"),
       "SEC 'BRK-B' vs Nasdaq/Cboe 'BRK.B'; no debt lines or EPS on a USD 1.2T balance sheet; stale dei shares.",
       1067983),
    _s("META", "Meta Platforms", ("large_tech", "edge_reporting"),
       "Name change (Facebook Inc until 2021-10-27) with the same CIK; no dei shares concept.", 1326801),
    _s("RIVN", "Rivian", ("loss_making", "edge_reporting"),
       "Negative earnings and gross profit; net_income (ProfitLoss) differs from net_income_to_common (NCI); "
       "listed 2021-11.", 1874178),
    _s("PTON", "Peloton", ("loss_making", "non_calendar_fy"),
       "Loss-making consumer name with a June fiscal year.", 1639825),
    _s("KOSS", "Koss", ("smaller_cap", "non_calendar_fy", "edge_reporting"),
       "Small cap; restated period in raw companyfacts (2017-Q3 revenue); sparse statements; June FY.", 56701),
    _s("COST", "Costco", ("non_calendar_fy",),
       "52/53-week fiscal year ending near 31 August.", 909832),
    _s("JPM", "JPMorgan Chase", ("financial",),
       "Bank: no operating-income line, different statement template (company_type financial)."),
    _s("TSM", "Taiwan Semiconductor", ("adr", "semiconductor", "edge_reporting"),
       "NYSE-listed ADR of a foreign private issuer: 20-F/IFRS expected gaps in us-gaap companyfacts."),
    _s("TSLA", "Tesla", ("amendment",),
       "10-K/A filings in 2025 and 2026 (amendments scan): original and amended filings stay distinct."),
)  # fmt: skip

SYMBOLS = tuple(s.symbol for s in MANIFEST)


def primary_symbol_for(symbol: str) -> str:
    """The designated primary listing of the issuer that ``symbol`` belongs to (itself unless it is a second class)."""
    by_symbol = {s.symbol: s for s in MANIFEST}
    entry = by_symbol[canonical_symbol(symbol)]
    group = [s for s in MANIFEST if s.issuer == entry.issuer]
    return next(s.symbol for s in group if s.primary_listing)


def manifest_digest() -> str:
    payload = [[s.symbol, s.issuer, list(s.roles), s.phase2_cik, s.primary_listing] for s in MANIFEST]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def coverage() -> dict[str, list[str]]:
    return {role: [s.symbol for s in MANIFEST if role in s.roles] for role in ROLES}
