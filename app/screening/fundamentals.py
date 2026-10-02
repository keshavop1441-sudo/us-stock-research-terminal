"""Normalised accounting lines from raw SEC facts: tag selection, period handling, TTM construction. Pure, no I/O.

Input is the as-reported SEC points stored in ``financial_facts`` (one row per concept, period and FILING;
restatements and amendments are separate rows). Output is a value per normalised line (``app.models.concepts``)
together with the tag, filing and flags it came from, or a stated reason why no reliable value exists. Nothing here
guesses: an absent or conflicting line is reported as such (the metric functions then return MISSING_INPUT), it is
never replaced by zero.

Rules (docs/data_coverage.yaml ``comparability_rules``; PHASE 3A methodology in README):
* Periods are identified by their own start/end dates, never by ``fy``/``fp`` (those describe the FILING: a 10-K for
  FY2025 also carries FY2024 and FY2023 comparatives labelled fy=2025). Period kinds come from
  ``metrics.period_kind``.
* Tag per period: the first candidate tag that reports the exact period wins; a lower-priority tag reporting the same
  period with a value more than 1% different makes the line unreliable (``TAG_CONFLICT``).
* Vintage: the CURRENT view uses the most recently filed vintage of a period. A year-over-year growth uses ONE filing
  for both periods (``same_filing_pair``). A restated period is flagged ``RESTATED_PERIOD``.
* TTM (trailing twelve months) of a flow line = latest fiscal year + current YTD - prior-year YTD, all from ONE tag,
  with the YTD periods aligned to the fiscal year by dates (+-7 days for 52/53-week calendars). Anything misaligned is
  NOT_COMPARABLE. If the latest report is a 10-K, TTM is that fiscal year. TTM EPS additionally needs a split guard
  (see ``ttm_eps``). A TTM SHARE COUNT is never built (a sum of weighted counts is meaningless; audit finding 4).
* Balance-sheet lines are instants taken at ONE date (the latest date at which cash or equity is reported).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.models.concepts import SPEC_BY_LINE, SPEC_BY_TAG
from app.screening.metrics import (
    FactPoint,
    MetricResult,
    MetricState,
    PeriodKind,
    period_kind,
    same_filing_pair,
    was_restated,
)

TAG_CONFLICT_TOLERANCE = 0.01
ALIGNMENT_DAYS = 7
YTD_KINDS = frozenset({PeriodKind.QUARTER, PeriodKind.HALF_YEAR_YTD, PeriodKind.NINE_MONTH_YTD})
FY_KINDS = frozenset({PeriodKind.FISCAL_YEAR})
INSTANT_KINDS = frozenset({PeriodKind.INSTANT})
SHARE_RATIO_LIMITS = (0.55, 1.8)  # outside this a split/consolidation (or a >45% buyback) changed the per-share basis
STALE_SHARES_DAYS = 400


@dataclass(frozen=True)
class Fact:
    """One stored SEC point (as reported in one filing)."""

    taxonomy: str
    concept: str
    unit: str
    value: float
    period_start: date | None
    period_end: date
    filed: date
    accession: str
    form: str | None = None
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    frame: str | None = None

    @property
    def kind(self) -> PeriodKind:
        return period_kind(self.period_start, self.period_end)

    def as_point(self) -> FactPoint:
        return FactPoint(self.value, self.period_start, self.period_end, self.filed, self.accession, self.form)


@dataclass(frozen=True)
class Selection:
    """A chosen value for one line and period, or the reason there is none."""

    fact: Fact | None
    tag: str | None = None
    flags: tuple[str, ...] = ()
    problem: str | None = None  # NOT_REPORTED | TAG_CONFLICT:a!=b

    @property
    def value(self) -> float | None:
        return self.fact.value if self.fact else None


@dataclass(frozen=True)
class Ttm:
    value: float
    period_end: date
    tag: str
    basis: str  # 'FY' or 'FY+YTD-YTD_PRIOR'
    components: tuple[Fact, ...]
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class TtmOutcome:
    ttm: Ttm | None
    state: MetricState = MetricState.OK
    reason: str | None = None

    def as_metric(self) -> MetricResult:
        if self.ttm is not None:
            return MetricResult(MetricState.OK, self.ttm.value, None, self.ttm.flags)
        return MetricResult(self.state, None, self.reason)


def _near(a: date, b: date, days: int = ALIGNMENT_DAYS) -> bool:
    return abs((a - b).days) <= days


def _years_apart(later: date, earlier: date) -> bool:
    return 350 <= (later - earlier).days <= 380


@dataclass
class StatementIndex:
    facts: Iterable[Fact]
    _by_line: dict[str, dict[str, list[Fact]]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for fact in self.facts:
            spec = SPEC_BY_TAG.get((fact.taxonomy, fact.concept))
            if spec is None or fact.unit != spec.unit:
                continue
            self._by_line.setdefault(spec.line, {}).setdefault(fact.concept, []).append(fact)

    def has_line(self, line: str) -> bool:
        return bool(self._by_line.get(line))

    def tag_facts(self, line: str, tag: str) -> list[Fact]:
        return self._by_line.get(line, {}).get(tag, [])

    def all_facts(self, line: str) -> list[Fact]:
        return [f for tag in SPEC_BY_LINE[line].tags for f in self.tag_facts(line, tag)]

    # --- single period -------------------------------------------------------------------------------------------

    def select(
        self,
        line: str,
        *,
        end: date,
        start: date | None = None,
        kinds: frozenset[PeriodKind] | None = None,
        tags: tuple[str, ...] | None = None,
        as_of: date | None = None,
        accession: str | None = None,
    ) -> Selection:
        """Value of ``line`` for the period ending ``end`` (and starting ``start`` if given), latest vintage, tag by
        priority. ``accession`` restricts the choice to the values ONE filing reported (same-filing comparisons)."""
        chosen: tuple[str, Fact, list[Fact]] | None = None
        others: list[tuple[str, Fact]] = []
        for tag in tags or SPEC_BY_LINE[line].tags:
            points = [
                f
                for f in self.tag_facts(line, tag)
                if f.period_end == end
                and (start is None or f.period_start == start)
                and (kinds is None or f.kind in kinds)
                and (as_of is None or f.filed <= as_of)
                and (accession is None or f.accession == accession)
            ]
            if not points:
                continue
            best = max(points, key=lambda f: (f.filed, f.accession))
            if chosen is None:
                chosen = (tag, best, points)
            else:
                others.append((tag, best))
        if chosen is None:
            return Selection(None, problem="NOT_REPORTED")
        tag, best, points = chosen
        for other_tag, other in others:
            scale = max(abs(best.value), abs(other.value))
            if scale and abs(best.value - other.value) / scale > TAG_CONFLICT_TOLERANCE:
                return Selection(None, problem=f"TAG_CONFLICT:{tag}!={other_tag}")
        flags = tuple(f"CONFIRMED_BY:{t}" for t, _ in others) + (
            ("RESTATED_PERIOD",) if was_restated([p.as_point() for p in points]) else ()
        )
        return Selection(best, tag, flags)

    def latest_end(self, line: str, kinds: frozenset[PeriodKind]) -> date | None:
        ends = [f.period_end for f in self.all_facts(line) if f.kind in kinds]
        return max(ends) if ends else None

    def fiscal_year_label(self, line: str, end: date) -> int | None:
        """``fy`` of the EARLIEST filing that reported this period (its original report, where fy is its own year)."""
        points = [f for f in self.all_facts(line) if f.period_end == end and f.kind in FY_KINDS and f.fiscal_year]
        return min(points, key=lambda f: (f.filed, f.accession)).fiscal_year if points else None

    # --- fiscal-year pairs from ONE filing -----------------------------------------------------------------------

    def annual_pair(
        self, line: str, now_end: date, *, tags: tuple[str, ...] | None = None
    ) -> tuple[Fact, Fact, str] | None:
        """(current, prior, tag) of fiscal-year values taken from the same filing; None if no filing reports both."""
        for tag in tags or SPEC_BY_LINE[line].tags:
            points = [f for f in self.tag_facts(line, tag) if f.kind in FY_KINDS]
            priors = sorted({f.period_end for f in points if _years_apart(now_end, f.period_end)})
            if not priors or not any(f.period_end == now_end for f in points):
                continue
            prior_end = priors[-1]
            pair = same_filing_pair([f.as_point() for f in points], now_end, prior_end)
            if pair is None:
                continue
            now_point, prior_point = pair
            lookup = {(f.period_end, f.accession): f for f in points}
            return lookup[(now_end, now_point.accession)], lookup[(prior_end, prior_point.accession)], tag
        return None

    # --- trailing twelve months ----------------------------------------------------------------------------------

    def ttm(self, line: str) -> TtmOutcome:
        """TTM of a flow line ending at the latest reported period (see module docstring)."""
        fy_end = self.latest_end(line, FY_KINDS)
        if fy_end is None:
            return TtmOutcome(None, MetricState.MISSING_INPUT, "NO_ANNUAL_VALUE")
        problems: list[str] = []
        for tag in SPEC_BY_LINE[line].tags:
            if not any(f.kind in FY_KINDS and f.period_end == fy_end for f in self.tag_facts(line, tag)):
                continue
            outcome = self._compose(line, tag, fy_end)
            if outcome.ttm is not None:
                return outcome
            problems.append(outcome.reason or "?")
            if outcome.state is not MetricState.MISSING_INPUT:
                return outcome
        return TtmOutcome(None, MetricState.MISSING_INPUT, problems[0] if problems else "NO_ANNUAL_VALUE")

    def _compose(self, line: str, tag: str, fy_end: date) -> TtmOutcome:
        fy = self.select(line, end=fy_end, kinds=FY_KINDS, tags=(tag,))
        if fy.fact is None:
            return TtmOutcome(None, MetricState.MISSING_INPUT, fy.problem)
        fy_start = fy.fact.period_start
        assert fy_start is not None
        # the YTD that starts the day after the fiscal year and ends latest (a 10-Q's cumulative period)
        later = [
            f
            for f in self.tag_facts(line, tag)
            if f.kind in YTD_KINDS
            and f.period_end > fy_end
            and f.period_start
            and _near(f.period_start, fy_end + timedelta(1))
        ]
        if not later:
            newer_unaligned = [f for f in self.tag_facts(line, tag) if f.kind in YTD_KINDS and f.period_end > fy_end]
            if newer_unaligned:  # interim data exists but does not start at the fiscal year end: refuse to mix
                return TtmOutcome(None, MetricState.NOT_COMPARABLE, "TTM_PERIODS_MISALIGNED")
            return TtmOutcome(Ttm(fy.fact.value, fy_end, tag, "FY", (fy.fact,), fy.flags))
        cur_end = max(f.period_end for f in later)
        cur_start = next(f.period_start for f in later if f.period_end == cur_end)
        cur = self.select(line, end=cur_end, start=cur_start, kinds=YTD_KINDS, tags=(tag,))
        if cur.fact is None:
            return TtmOutcome(None, MetricState.MISSING_INPUT, cur.problem)
        return self._compose_at(line, tag, fy_end, cur.fact, cur.flags)

    def ttm_prior_year(self, line: str, current: Ttm) -> TtmOutcome:
        """The TTM one year before ``current`` (same tag, same construction), for TTM year-over-year growth."""
        tag = current.tag
        if current.basis == "FY":
            fy = current.components[0]
            pair = self.annual_pair(line, fy.period_end, tags=(tag,))
            if pair is None:
                return TtmOutcome(None, MetricState.MISSING_INPUT, "PRIOR_YEAR_NOT_IN_SAME_FILING")
            return TtmOutcome(Ttm(pair[1].value, pair[1].period_end, tag, "FY", (pair[1],), ()))
        fy, cur, prior = current.components
        prev_fy_end = max(
            (
                f.period_end
                for f in self.tag_facts(line, tag)
                if f.kind in FY_KINDS and _years_apart(fy.period_end, f.period_end)
            ),
            default=None,
        )
        if prev_fy_end is None:
            return TtmOutcome(None, MetricState.MISSING_INPUT, "PRIOR_FISCAL_YEAR_MISSING")
        return self._compose_at(line, tag, prev_fy_end, prior)

    def _compose_at(self, line: str, tag: str, fy_end: date, cur: Fact, cur_flags: tuple[str, ...] = ()) -> TtmOutcome:
        """TTM anchored at a given (already chosen) YTD period ``cur`` of the fiscal year after ``fy_end``."""
        fy = self.select(line, end=fy_end, kinds=FY_KINDS, tags=(tag,))
        if fy.fact is None or fy.fact.period_start is None or cur.period_start is None:
            return TtmOutcome(None, MetricState.MISSING_INPUT, fy.problem or "NOT_REPORTED")
        if not _near(cur.period_start, fy_end + timedelta(1)):
            return TtmOutcome(None, MetricState.NOT_COMPARABLE, "TTM_PERIODS_MISALIGNED")
        prior_candidates = [
            f
            for f in self.tag_facts(line, tag)
            if f.kind == cur.kind
            and f.period_start
            and _years_apart(cur.period_end, f.period_end)
            and _years_apart(cur.period_start, f.period_start)
            and _near(f.period_start, fy.fact.period_start)
        ]
        if not prior_candidates:
            return TtmOutcome(None, MetricState.MISSING_INPUT, "PRIOR_YEAR_YTD_MISSING")
        prior_end = max(f.period_end for f in prior_candidates)
        prior_start = next(f.period_start for f in prior_candidates if f.period_end == prior_end)
        prior = self.select(line, end=prior_end, start=prior_start, kinds=YTD_KINDS, tags=(tag,))
        if prior.fact is None:
            return TtmOutcome(None, MetricState.MISSING_INPUT, "PRIOR_YEAR_YTD_MISSING")
        return TtmOutcome(
            Ttm(
                fy.fact.value + cur.value - prior.fact.value,
                cur.period_end,
                tag,
                "FY+YTD-YTD_PRIOR",
                (fy.fact, cur, prior.fact),
                tuple(dict.fromkeys(("COMPOSED:FY+YTD-YTD_PRIOR", *fy.flags, *cur_flags, *prior.flags))),
            )
        )

    def ttm_eps(self) -> TtmOutcome:
        """TTM diluted EPS = FY + YTD - YTD_prior (sum of per-share figures, flagged), with a split guard.

        Per-share figures are on the basis of THEIR filing. A 10-Q filed after a stock split reports the YTD figures
        post-split while the latest 10-K's fiscal-year EPS is still pre-split (audit: NVDA FY2024 EPS 1.19 vs 11.93).
        Mixing them silently would give a P/E off by the split ratio. The guard compares weighted diluted shares of
        the YTD period with those of the fiscal year; a ratio outside 0.55-1.8 (a 10:1 split gives ~10) means the
        basis changed -> NOT_COMPARABLE. If the share counts cannot be found the composition cannot be checked ->
        MISSING_INPUT. A TTM that is simply the latest fiscal year needs no guard.
        """
        outcome = self.ttm("diluted_eps")
        if outcome.ttm is None or outcome.ttm.basis == "FY":
            return outcome
        fy, cur, _ = outcome.ttm.components
        fy_shares = self.select("weighted_diluted_shares", end=fy.period_end, start=fy.period_start, kinds=FY_KINDS)
        cur_shares = self.select("weighted_diluted_shares", end=cur.period_end, start=cur.period_start, kinds=YTD_KINDS)
        if fy_shares.fact is None or cur_shares.fact is None or fy_shares.value == 0:
            return TtmOutcome(None, MetricState.MISSING_INPUT, "SPLIT_GUARD_UNAVAILABLE")
        ratio = cur_shares.fact.value / fy_shares.fact.value
        low, high = SHARE_RATIO_LIMITS
        if not low <= ratio <= high:
            return TtmOutcome(None, MetricState.NOT_COMPARABLE, "POSSIBLE_SPLIT_BASIS_CHANGE")
        flags = (*outcome.ttm.flags, "PER_SHARE_SUM_OF_PERIODS")
        return TtmOutcome(
            Ttm(
                outcome.ttm.value,
                outcome.ttm.period_end,
                outcome.ttm.tag,
                outcome.ttm.basis,
                outcome.ttm.components,
                flags,
            )
        )

    # --- balance sheet and shares ----------------------------------------------------------------------------------

    def balance_sheet(self) -> "BalanceSheet":
        ends = [e for line in ("cash", "equity") if (e := self.latest_end(line, INSTANT_KINDS))]
        if not ends:
            return BalanceSheet(None, {}, {})
        end = max(ends)
        values: dict[str, Selection] = {}
        for line, spec in SPEC_BY_LINE.items():
            if spec.kind == "instant" and line != "shares_outstanding":
                values[line] = self.select(line, end=end, kinds=INSTANT_KINDS)
        conflicts = {
            line: sel.problem for line, sel in values.items() if sel.problem and sel.problem.startswith("TAG_CONFLICT")
        }
        return BalanceSheet(end, values, conflicts)

    def shares_outstanding(self, as_of: date) -> tuple[float | None, str | None]:
        """(shares, None) for a reliable single-class cover-page count, else (None, reason)."""
        points = self.all_facts("shares_outstanding")
        if not points:
            return None, "NOT_REPORTED"
        latest_end = max(p.period_end for p in points)
        if (as_of - latest_end).days > STALE_SHARES_DAYS:
            return None, f"STALE_SHARES:{latest_end.isoformat()}"
        at_end = [p for p in points if p.period_end == latest_end]
        newest = max(p.filed for p in at_end)
        values = {p.value for p in at_end if p.filed == newest}
        if len(values) > 1:  # several classes collapsed into one undimensioned series: ambiguous
            return None, "MULTI_CLASS_AMBIGUOUS"
        return values.pop(), None


@dataclass(frozen=True)
class BalanceSheet:
    end: date | None
    values: dict[str, Selection]
    conflicts: dict[str, str | None]

    @property
    def present(self) -> bool:
        return self.end is not None

    def value(self, line: str) -> float | None:
        sel = self.values.get(line)
        return sel.value if sel else None
