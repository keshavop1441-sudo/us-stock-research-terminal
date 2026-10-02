"""Raw SEC XBRL facts: the canonical provenance layer for accounting data.

OpenBB's statement commands return one normalised row per period and drop the filing metadata (accession number,
filed date, form, vintage). For accounting facts the project therefore keeps the SEC's own ``companyfacts`` points as
the source of record and treats OpenBB statements as a convenience view only (docs/data_coverage.yaml,
``sec_provenance``).

``RawFactPoint`` holds every field a ``companyfacts`` point carries (``accn, end, filed, form, fp, frame, fy, start,
val``) plus the identity of the series it came from (cik, taxonomy, concept, unit) and a deterministic source
reference. ``to_record`` converts to the write-side ``FinancialFactRecord``; ``frame`` has no column yet (schema gap
G7) so it is preserved on the raw object and reported, never silently dropped.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.identifiers import normalize_cik
from app.models.records import FinancialFactRecord

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
FRAMES_URL = "https://data.sec.gov/api/xbrl/frames/{taxonomy}/{concept}/{unit}/{frame}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_number}/{accession_nodash}/"
RAW_POINT_KEYS = frozenset({"accn", "end", "filed", "form", "fp", "frame", "fy", "start", "val"})


class RawFactPoint(BaseModel):
    """One as-reported XBRL fact. ``start is None`` means an instant (balance-sheet date)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cik: str
    taxonomy: str
    concept: str
    unit: str
    value: float
    period_start: date | None
    period_end: date
    filed: date
    form: str | None
    fiscal_year: int | None
    fiscal_period: str | None
    frame: str | None
    accession: str | None

    @field_validator("cik", mode="before")
    @classmethod
    def _cik(cls, value: int | str) -> str:
        return normalize_cik(value)

    @property
    def is_instant(self) -> bool:
        return self.period_start is None

    @property
    def source_ref(self) -> str:
        """Deterministic reference to where this exact point can be re-fetched (the series document, not a copy)."""
        return COMPANYFACTS_URL.format(cik=self.cik) + f"#{self.taxonomy}/{self.concept}/{self.unit}"

    @property
    def filing_url(self) -> str | None:
        """Directory of the filing that reported the point (derivable from the accession number)."""
        if not self.accession:
            return None
        return ARCHIVE_URL.format(cik_number=int(self.cik), accession_nodash=self.accession.replace("-", ""))

    def to_record(self, source_id: int | None = None) -> FinancialFactRecord:
        return FinancialFactRecord(
            cik=self.cik,
            taxonomy=self.taxonomy,
            concept=self.concept,
            unit=self.unit,
            value=self.value,
            period_start=self.period_start,
            period_end=self.period_end,
            fiscal_year=self.fiscal_year,
            fiscal_period=self.fiscal_period,
            form=self.form,
            filed_date=self.filed,
            accession_no=self.accession,
            frame=self.frame,
            source_id=source_id,
        )


def parse_companyfacts_point(
    cik: int | str, taxonomy: str, concept: str, unit: str, point: dict[str, object]
) -> RawFactPoint:
    """Parse one element of ``facts[taxonomy][concept]["units"][unit]`` exactly as the SEC publishes it.

    Unknown keys are rejected (the SEC changing its schema must be noticed, not ignored); ``start`` is absent for
    instants; ``frame`` is absent for points that are not the canonical value of a calendar frame.
    """
    unknown = set(point) - RAW_POINT_KEYS
    if unknown:
        raise ValueError(f"unexpected companyfacts keys: {sorted(unknown)}")
    missing = {"end", "val", "filed"} - set(point)
    if missing:
        raise ValueError(f"companyfacts point lacks required keys: {sorted(missing)}")
    start = point.get("start")
    return RawFactPoint(
        cik=cik,  # type: ignore[arg-type]
        taxonomy=taxonomy,
        concept=concept,
        unit=unit,
        value=float(point["val"]),  # type: ignore[arg-type]
        period_start=date.fromisoformat(str(start)) if start else None,
        period_end=date.fromisoformat(str(point["end"])),
        filed=date.fromisoformat(str(point["filed"])),
        form=point.get("form") or None,  # type: ignore[arg-type]
        fiscal_year=int(point["fy"]) if point.get("fy") is not None else None,  # type: ignore[arg-type]
        fiscal_period=point.get("fp") or None,  # type: ignore[arg-type]
        frame=point.get("frame") or None,  # type: ignore[arg-type]
        accession=point.get("accn") or None,  # type: ignore[arg-type]
    )
