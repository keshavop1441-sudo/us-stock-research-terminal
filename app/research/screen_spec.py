"""The structured screen specification: what Claude produces from a natural-language request and Python executes.

Claude TRANSLATES; it never filters. This module validates the translation strictly (unknown fields, unknown metrics,
ambiguous units, impossible bounds and mixed classification taxonomies are all rejected with a precise message), so a
mistranslation fails loudly instead of silently screening on the wrong thing.

Example (``down 30-50 %, revenue growth > 15 %, positive FCF, P/S < 5, improving gross margin``)::

    {"name": "tech drawdown growth screen",
     "classification": {"taxonomy": "nasdaq", "sector": "Technology"},
     "criteria": [
       {"metric": "drawdown_from_52w_high", "op": "between", "low": -50, "high": -30, "unit": "percent"},
       {"metric": "revenue_growth_yoy", "op": "gt", "value": 15, "unit": "percent"},
       {"metric": "fcf", "op": "gt", "value": 0},
       {"metric": "price_to_sales", "op": "lt", "value": 5},
       {"metric": "gross_margin_change_yoy", "op": "gt", "value": 0, "unit": "fraction"}]}
"""

import math
from datetime import date
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.models.identifiers import normalize_ticker
from app.models.symbols import canonical_symbol
from app.research.catalog import CATALOG

MAX_CRITERIA = 20
MAX_UNIVERSE_SYMBOLS = 200
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Op = Literal["gt", "gte", "lt", "lte", "eq", "between"]
OP_SYMBOL = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "=="}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _finite(value: float | None, label: str) -> float | None:
    if value is not None and not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return value


class Criterion(_Strict):
    """One numeric condition on a catalogued metric. ``between`` is inclusive on both ends."""

    metric: str
    op: Op
    value: float | None = None
    low: float | None = None
    high: float | None = None
    unit: Literal["fraction", "percent", "native"] = "native"
    label: Text | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        spec = CATALOG.get(self.metric)
        if spec is None:
            raise ValueError(f"unknown metric {self.metric!r}; run `research.py catalog` for the supported names")
        for name in ("value", "low", "high"):
            _finite(getattr(self, name), name)
        if self.op == "between":
            if self.low is None or self.high is None or self.value is not None:
                raise ValueError("op 'between' needs low and high (and no value)")
            if self.low > self.high:
                raise ValueError(f"between: low {self.low} is above high {self.high}")
        elif self.value is None or self.low is not None or self.high is not None:
            raise ValueError(f"op {self.op!r} needs a value (and no low/high)")
        if spec.unit == "fraction":
            if self.unit == "native":
                raise ValueError(
                    f"{self.metric} is a fraction metric: say unit 'percent' (15 = 15%) or 'fraction' (0.15 = 15%)"
                )
        elif self.unit != "native":
            raise ValueError(f"{self.metric} is not a fraction metric ({spec.unit}); leave unit as 'native'")
        return self

    def scaled(self) -> tuple[float | None, float | None, float | None]:
        """(value, low, high) in the metric's stored unit (percent thresholds divided by 100)."""
        factor = 0.01 if self.unit == "percent" else 1.0

        def conv(x: float | None) -> float | None:
            return None if x is None else x * factor

        return conv(self.value), conv(self.low), conv(self.high)


class ClassificationFilter(_Strict):
    """A filter in ONE named taxonomy. Nasdaq's sector/industry labels and SEC SIC codes are different systems and are
    never converted into each other; GICS is not supported."""

    taxonomy: Literal["nasdaq", "sec_sic"]
    sector: Text | None = None
    industries: list[Text] = Field(default_factory=list, max_length=20)
    exclude_industries: list[Text] = Field(default_factory=list, max_length=20)
    sic_codes: list[Annotated[str, StringConstraints(pattern=r"^\d{1,4}$")]] = Field(
        default_factory=list, max_length=40
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.taxonomy == "nasdaq":
            if self.sic_codes:
                raise ValueError("sic_codes belong to taxonomy 'sec_sic', not 'nasdaq'")
            if not (self.sector or self.industries or self.exclude_industries):
                raise ValueError("a nasdaq filter needs sector and/or industries/exclude_industries")
        else:
            if self.sector or self.industries or self.exclude_industries:
                raise ValueError("SEC SIC has no sector/industry labels to filter on; use sic_codes only")
            if not self.sic_codes:
                raise ValueError("a sec_sic filter needs sic_codes")
        return self


class UniverseSpec(_Strict):
    """Which securities are screened. Default: every security currently in the local database (the ingested working
    set - the full market universe is NOT loaded). ``symbols`` restricts it to a subset."""

    symbols: list[str] | None = Field(default=None, max_length=MAX_UNIVERSE_SYMBOLS)

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = list(dict.fromkeys(canonical_symbol(normalize_ticker(v)) for v in value))
        if not cleaned:
            raise ValueError("symbols must not be empty (omit it to screen everything ingested)")
        return cleaned


class ScreenSpec(_Strict):
    name: Text = "screen"
    as_of: date | None = None
    universe: UniverseSpec = Field(default_factory=UniverseSpec)
    classification: ClassificationFilter | None = None
    criteria: list[Criterion] = Field(default_factory=list, max_length=MAX_CRITERIA)

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not self.criteria and self.classification is None:
            raise ValueError("a screen needs at least one criterion or a classification filter")
        return self
