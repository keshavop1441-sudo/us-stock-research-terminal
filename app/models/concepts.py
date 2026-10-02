"""The P0 accounting concepts: which SEC XBRL tags may supply each normalised line, in priority order.

Evidence status (Phase 2 audit, ``docs/data_coverage.yaml``): the audit probed
``RevenueFromContractWithCustomerExcludingAssessed Tax`` / ``Revenues`` / ``SalesRevenueNet`` (revenue),
``PaymentsToAcquirePropertyPlantAndEquipment`` (capex), ``NetCashProvidedByUsedInOperatingActivities``,
``EarningsPerShareDiluted``, ``NetIncomeLoss`` (attributable to parent; ``ProfitLoss`` is the consolidated figure and
is deliberately NOT a candidate), ``LongTermDebtNoncurrent`` / ``LongTermDebt`` and dei
``EntityCommonStockSharesOutstanding`` against live companyfacts. The remaining tags (``GrossProfit``,
``OperatingIncomeLoss``, ``StockholdersEquity``, cash, short-term investments, ``ShortTermBorrowings``,
``LongTermDebtCurrent``, weighted diluted shares) are the standard us-gaap names for the metric dictionary's lines but
were NOT individually probed live: their status stays API_SHAPE/NOT_VERIFIED until the P0 live run reports them (see
docs/data_coverage.yaml ``p0_pilot``).

Rules that apply to every line (``app/screening/fundamentals.py``):
* the tag is chosen PER PERIOD (a company may change tag over time); the first candidate that reports the period wins;
* if a lower-priority candidate also reports that period with a materially different value the line is NOT reliable
  (``TAG_CONFLICT``) and the metric is MISSING_INPUT - a single tag is never trusted blindly, and never guessed;
* nothing is derived from an absent line (no capex = 0, no debt = 0).
"""

from dataclasses import dataclass
from typing import Literal

Kind = Literal["duration", "instant"]


@dataclass(frozen=True)
class ConceptSpec:
    line: str  # normalised line name used by the metric functions
    taxonomy: str
    tags: tuple[str, ...]  # priority order
    unit: str
    kind: Kind
    note: str = ""


US_GAAP = "us-gaap"

SPECS: tuple[ConceptSpec, ...] = (
    ConceptSpec(
        "revenue",
        US_GAAP,
        (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
        ),
        "USD",
        "duration",
    ),  # fmt: skip
    ConceptSpec("gross_profit", US_GAAP, ("GrossProfit",), "USD", "duration"),
    ConceptSpec("operating_income", US_GAAP, ("OperatingIncomeLoss",), "USD", "duration"),
    ConceptSpec(
        "net_income_to_common",
        US_GAAP,
        ("NetIncomeLoss",),
        "USD",
        "duration",
        "attributable to the parent; ProfitLoss (consolidated, incl. non-controlling interests) is a different line",
    ),  # fmt: skip
    ConceptSpec("diluted_eps", US_GAAP, ("EarningsPerShareDiluted",), "USD/shares", "duration"),
    ConceptSpec(
        "weighted_diluted_shares",
        US_GAAP,
        ("WeightedAverageNumberOfDilutedSharesOutstanding",),
        "shares",
        "duration",
        "used only as a split/basis-change guard for per-share TTM sums, never as a TTM share count",
    ),  # fmt: skip
    ConceptSpec("operating_cash_flow", US_GAAP, ("NetCashProvidedByUsedInOperatingActivities",), "USD", "duration"),
    ConceptSpec(
        "capex",
        US_GAAP,
        ("PaymentsToAcquirePropertyPlantAndEquipment",),
        "USD",
        "duration",
        "gross payments (positive); the only capex tag the audit established; absent -> FCF is MISSING_INPUT",
    ),  # fmt: skip
    ConceptSpec("cash", US_GAAP, ("CashAndCashEquivalentsAtCarryingValue", "Cash"), "USD", "instant"),
    ConceptSpec(
        "short_term_investments", US_GAAP, ("ShortTermInvestments", "MarketableSecuritiesCurrent"), "USD", "instant"
    ),
    ConceptSpec(
        "short_term_debt",
        US_GAAP,
        ("ShortTermBorrowings", "CommercialPaper"),
        "USD",
        "instant",
        "ShortTermBorrowings first; CommercialPaper (a short-term borrowing instrument, how e.g. Apple reports its "
        "short-term debt) only where the first is not reported for the date. Two tags that disagree by more than 1% "
        "for one date -> TAG_CONFLICT (unavailable). DebtCurrent is NOT mapped: it already contains the current "
        "portion of long-term debt and would double count",
    ),  # fmt: skip
    ConceptSpec("current_portion_long_term_debt", US_GAAP, ("LongTermDebtCurrent",), "USD", "instant"),
    ConceptSpec("long_term_debt", US_GAAP, ("LongTermDebtNoncurrent",), "USD", "instant"),
    ConceptSpec("equity", US_GAAP, ("StockholdersEquity",), "USD", "instant", "parent shareholders, excl. NCI"),
    ConceptSpec(
        "shares_outstanding",
        "dei",
        ("EntityCommonStockSharesOutstanding",),
        "shares",
        "instant",
        "cover-page count; reliable only for single-class issuers with a recent value",
    ),  # fmt: skip
)

# Accounting taxonomies the P0 concept set is defined for. An issuer whose companyfacts carry ONLY an unsupported
# accounting taxonomy (e.g. a foreign private issuer reporting under IFRS in a non-USD currency) is a deliberate,
# documented coverage gap: no fact is stored, no mapping to us-gaap is invented, and its fundamentals are
# MISSING_INPUT with the reason UNSUPPORTED_TAXONOMY:<taxonomy> (never 0). See docs/data_coverage.md section 13.
SUPPORTED_ACCOUNTING_TAXONOMIES = frozenset({US_GAAP})
KNOWN_UNSUPPORTED_ACCOUNTING_TAXONOMIES = frozenset({"ifrs-full"})

SPEC_BY_LINE = {spec.line: spec for spec in SPECS}
# (taxonomy, tag) -> spec: what an ingested companyfacts point must match to be stored.
SPEC_BY_TAG = {(spec.taxonomy, tag): spec for spec in SPECS for tag in spec.tags}
# Filings whose facts are ingested. Amendments are kept as separate rows (new accession), never merged.
ACCOUNTING_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A"})
# Filings indexed from submissions (identity of what was filed, incl. amendments).
INDEXED_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"})
