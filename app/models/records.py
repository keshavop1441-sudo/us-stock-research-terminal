"""Typed, validated records accepted by the write repository.

These are the ONLY shapes the write path accepts (no free-form dicts, no SQL). Validation happens
here: CIKs and tickers are canonicalised via ``app.models.identifiers``, identity strings are
checked, and each record exposes the business key that makes reloading it idempotent.
The business keys are documented in ``app.database.schema``.
"""

import re
from datetime import date
from typing import Annotated

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.models.identifiers import normalize_cik, normalize_optional_cik, normalize_ticker

KEY_SEPARATOR = "|"


def _identity_text(value: str) -> str:
    """Stripped, non-empty text that is safe to use inside a ``|``-separated business key."""
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    if KEY_SEPARATOR in value:
        raise ValueError(f"must not contain '{KEY_SEPARATOR}' (percent-encode it)")
    return value


def _optional_identity_text(value: str | None) -> str | None:
    return None if value is None else _identity_text(value)


# Before-validators: they receive the raw input (an int CIK, an unstripped ticker) and are the single
# normalisation point; pydantic's own type check then sees the canonical string.
Cik = Annotated[str, BeforeValidator(normalize_cik)]
OptionalCik = Annotated[str | None, BeforeValidator(normalize_optional_cik)]
Ticker = Annotated[str, BeforeValidator(normalize_ticker)]
IdentityText = Annotated[str, AfterValidator(_identity_text)]
OptionalIdentityText = Annotated[str | None, AfterValidator(_optional_identity_text)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]  # NaN/inf are "missing": use None instead


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class SourceRecord(Record):
    provider: IdentityText
    dataset: IdentityText
    url: str | None = None
    content_hash: str | None = None
    detail: str | None = None


class SecurityRecord(Record):
    ticker: Ticker
    cik: OptionalCik = None
    name: str | None = None
    exchange: str | None = None
    security_type: str | None = None
    sector: str | None = None
    industry: str | None = None
    sic: str | None = None
    is_active: bool | None = None


class FinancialFactRecord(Record):
    cik: Cik
    taxonomy: IdentityText
    concept: IdentityText
    unit: IdentityText
    value: FiniteFloat
    period_start: date | None = None  # None for point-in-time facts
    period_end: date
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    form: str | None = None
    filed_date: date | None = None
    accession_no: OptionalIdentityText = None
    source_id: int | None = None

    @property
    def key(self) -> str:
        return KEY_SEPARATOR.join(
            [
                self.cik,
                self.taxonomy,
                self.concept,
                self.unit,
                self.period_start.isoformat() if self.period_start else "",
                self.period_end.isoformat(),
                self.accession_no or "",
            ]
        )


class FilingRecord(Record):
    accession_no: IdentityText
    cik: Cik
    form: IdentityText
    filing_date: date
    report_date: date | None = None
    primary_document: str | None = None
    description: str | None = None
    url: str | None = None
    source_id: int | None = None


class PriceRecord(Record):
    security_id: int
    trade_date: date
    open: FiniteFloat | None = None
    high: FiniteFloat | None = None
    low: FiniteFloat | None = None
    close: FiniteFloat | None = None
    adj_close: FiniteFloat | None = None
    volume: int | None = None
    source_id: int | None = None


class EarningsRecord(Record):
    cik: Cik
    fiscal_year: int
    fiscal_period: IdentityText
    report_date: date
    eps_actual: FiniteFloat | None = None
    eps_estimate: FiniteFloat | None = None
    revenue_actual: FiniteFloat | None = None
    revenue_estimate: FiniteFloat | None = None
    source_id: int | None = None


def normalize_holder_name(name: str) -> str:
    """Case-folded, whitespace-collapsed holder name used as identity when no holder CIK is known.

    The key separator is treated like whitespace so any real-world name yields a valid key.
    """
    return re.sub(rf"[\s{re.escape(KEY_SEPARATOR)}]+", " ", name).strip().casefold()


class OwnershipRecord(Record):
    cik: Cik  # the issuer
    holder_type: IdentityText  # 'insider', 'institution', 'beneficial_owner'
    holder_name: Annotated[str, Field(min_length=1)]
    holder_cik: OptionalCik = None
    as_of_date: date
    accession_no: OptionalIdentityText = None
    line_no: int = Field(default=0, ge=0)
    shares: FiniteFloat | None = None
    value_usd: FiniteFloat | None = None
    shares_change: FiniteFloat | None = None
    transaction_code: str | None = None
    form: str | None = None
    source_id: int | None = None

    @model_validator(mode="after")
    def _holder_must_be_identifiable(self) -> "OwnershipRecord":
        self.holder_key  # noqa: B018 - raises ValueError (-> ValidationError) when no usable identity exists
        return self

    @property
    def holder_key(self) -> str:
        return _identity_text(self.holder_cik or normalize_holder_name(self.holder_name))

    @property
    def key(self) -> str:
        return KEY_SEPARATOR.join(
            [
                self.cik,
                self.holder_type,
                self.holder_key,
                self.as_of_date.isoformat(),
                self.accession_no or "",
                str(self.line_no),
            ]
        )


class EventRecord(Record):
    cik: OptionalCik = None
    event_type: IdentityText
    source_ref: IdentityText  # stable reference from the source: URL, accession no. + item, provider id
    event_date: date | None = None
    title: Annotated[str, Field(min_length=1)]
    summary: str | None = None
    url: str | None = None
    accession_no: str | None = None
    source_id: int | None = None

    @property
    def key(self) -> str:
        return KEY_SEPARATOR.join([self.cik or "", self.event_type, self.source_ref])


__all__ = [
    "EarningsRecord",
    "EventRecord",
    "FilingRecord",
    "FinancialFactRecord",
    "OwnershipRecord",
    "PriceRecord",
    "SecurityRecord",
    "SourceRecord",
]
