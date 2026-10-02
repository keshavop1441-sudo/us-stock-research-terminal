"""Industry classification without silent conversion between taxonomies.

Three different things are called "sector/industry" and none is converted into another here:

* ``nasdaq``  - Nasdaq's own labels (``sector`` such as "Technology", ``industry`` free text such as "Semiconductors").
* ``sec_sic`` - the SEC's Standard Industrial Classification of the issuer (a 4-digit code and its description). SIC has
  NO sector concept.
* GICS        - not used. Nothing in this project produces or assumes GICS.

A classification keeps its source system next to the raw values. A filter is written for ONE system; applying it to a
classification from another system is an error (``TaxonomyError``), never a guess. Missing classification -> ``None``
(unknown), which a screen treats as "cannot be evaluated", exactly like any other missing metric.
"""

from collections.abc import Iterable
from dataclasses import dataclass

NASDAQ = "nasdaq"
SEC_SIC = "sec_sic"
SYSTEMS = (NASDAQ, SEC_SIC)


class TaxonomyError(ValueError):
    """A filter and a classification belong to different taxonomies, or a filter asks a taxonomy for what it lacks."""


@dataclass(frozen=True)
class Classification:
    """The raw classification of one issuer/security as a source reported it."""

    system: str
    sector: str | None = None  # nasdaq only
    industry: str | None = None  # nasdaq: industry label; sec_sic: the SIC description
    code: str | None = None  # sec_sic only: the 4-digit SIC code

    def __post_init__(self) -> None:
        if self.system not in SYSTEMS:
            raise TaxonomyError(f"unknown classification system {self.system!r}")
        if self.system == SEC_SIC and self.sector is not None:
            raise TaxonomyError("SEC SIC has no sector; refusing to store one")
        if self.system == NASDAQ and self.code is not None:
            raise TaxonomyError("Nasdaq classification has no code")


def _norm(value: str | None) -> str | None:
    text = (value or "").strip().casefold()
    return text or None


def nasdaq_classification(sector: str | None, industry: str | None) -> Classification | None:
    """Nasdaq's raw labels; the literal string 'N/A' (returned for e.g. BRK.B) means unknown, so it is not stored."""
    clean = [None if _norm(v) in (None, "n/a") else v.strip() for v in (sector, industry)]
    return None if clean == [None, None] else Classification(NASDAQ, sector=clean[0], industry=clean[1])


def sic_classification(code: str | int | None, description: str | None) -> Classification | None:
    text = None if code is None else str(code).strip()
    if not text:
        return None
    return Classification(SEC_SIC, industry=(description or "").strip() or None, code=text.zfill(4))


def matches_nasdaq(
    classification: Classification | None,
    *,
    sector: str | None = None,
    industries: Iterable[str] = (),
    exclude_industries: Iterable[str] = (),
) -> bool | None:
    """Nasdaq-taxonomy filter: exact (case-insensitive) match on Nasdaq's own sector and industry strings."""
    if classification is None:
        return None
    if classification.system != NASDAQ:
        raise TaxonomyError(f"a Nasdaq filter cannot be applied to a {classification.system!r} classification")
    if sector is not None:
        if _norm(classification.sector) is None:
            return None
        if _norm(classification.sector) != _norm(sector):
            return False
    wanted = {_norm(i) for i in industries}
    excluded = {_norm(i) for i in exclude_industries}
    if wanted or excluded:
        industry = _norm(classification.industry)
        if industry is None:
            return None
        if wanted and industry not in wanted:
            return False
        if industry in excluded:
            return False
    return True


def matches_sic(classification: Classification | None, *, codes: Iterable[str | int]) -> bool | None:
    """SIC filter: exact match on the 4-digit code. SIC has no 'sector', so there is no sector parameter by design."""
    if classification is None:
        return None
    if classification.system != SEC_SIC:
        raise TaxonomyError(f"a SIC filter cannot be applied to a {classification.system!r} classification")
    return classification.code in {str(c).zfill(4) for c in codes}
