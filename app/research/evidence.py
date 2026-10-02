"""The evidence packet: what Claude receives instead of database internals.

A packet has one SECTION per kind of evidence. Every section reports a ``status`` and a list of ``items``; every item
keeps its source, source type, date, company, a short factual statement and provenance. Missing is explicit:

    section status  OK                         items present
                    NONE_FOUND                 the source was consulted and holds nothing in the window
                    NOT_COLLECTED              this engine did not (or could not) consult a source; ``guidance``
                                               says what Claude may do (web research WITH attribution and dates)
                    UNAVAILABLE / UNSUPPORTED  the data class cannot be provided (e.g. IFRS issuer fundamentals)

Nothing here fabricates: an item exists only because a provider/filing says so, and its ``content`` states the filing
fact (form, date, 8-K item codes), never an interpretation of what the filing means.
"""

import hashlib
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.sec_facts import ARCHIVE_URL

# Named tuple, not `except TypeError, ValueError:` (3.14-only syntax; see app/database/locking.py).
_BAD_DATE = (TypeError, ValueError)

SECTIONS = (
    "identity",
    "price",
    "fundamentals",
    "valuation",
    "balance_sheet",
    "ownership",
    "insiders",
    "filings",
    "earnings",
    "news_events",
    "contracts_customers",
    "partnerships",
    "mna",
    "legal_regulatory",
    "government_awards",
    "other",
)
SourceType = Literal[
    "SEC_FILING", "SEC_XBRL_FACTS", "SEC_IDENTITY", "MARKET_DATA_PROVIDER", "DERIVED_METRIC", "LOCAL_DATABASE"
]


class Company(BaseModel):
    model_config = ConfigDict(frozen=True)
    ticker: str
    cik: str | None = None
    name: str | None = None


class EvidenceItem(BaseModel):
    """One piece of evidence. ``as_of`` is the date the fact is about; ``filed`` the date it became public."""

    model_config = ConfigDict(frozen=True)
    id: str
    section: str
    company: Company
    source: str  # provider / publisher ('sec', 'nasdaq', 'cboe', 'derived')
    source_type: SourceType
    as_of: date | None = None
    filed: date | None = None
    content: str
    data: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    caveats: list[str] = Field(default_factory=list)


def item_id(section: str, ticker: str, *parts: object) -> str:
    digest = hashlib.sha256("|".join(str(p) for p in (section, ticker, *parts)).encode()).hexdigest()[:12]
    return f"{section}:{ticker}:{digest}"


def empty_packet(company: Company, as_of: date, generated_at: datetime) -> dict[str, Any]:
    return {
        "company": company.model_dump(),
        "as_of": as_of,
        "generated_at": generated_at,
        "sections": {
            name: {"status": "NOT_COLLECTED", "items": [], "missing": [], "guidance": None} for name in SECTIONS
        },
        "limits": [],
    }


def set_section(
    packet: dict[str, Any],
    name: str,
    items: Iterable[EvidenceItem],
    *,
    status: str | None = None,
    missing: Iterable[dict[str, Any]] = (),
    guidance: str | None = None,
) -> None:
    section = packet["sections"][name]
    section["items"] = [i.model_dump() for i in items]
    section["missing"] = list(missing)
    section["guidance"] = guidance
    section["status"] = status or ("OK" if section["items"] else "NONE_FOUND")


# --- SEC filing events: deterministic classification from the form type and the 8-K item codes ---------------------
#
# The classification only says which SECTION a filing belongs to. It never interprets the content: an 8-K Item 1.01
# is "entry into a material definitive agreement" - whether that is a customer contract, a financing or a partnership
# is NOT decided here (``caveats`` says so) and Claude must read the filing before saying more.

ANNUAL_QUARTERLY_FORMS = frozenset({"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"})
INSIDER_FORMS = frozenset({"3", "3/A", "4", "4/A", "5", "5/A", "144", "144/A"})
OWNERSHIP_FORMS = frozenset(
    {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"}
)
MNA_FORMS = frozenset(
    {
        "SC TO-T",
        "SC TO-T/A",
        "SC TO-I",
        "SC 14D9",
        "SC 14D9/A",
        "DEFM14A",
        "PREM14A",
        "S-4",
        "S-4/A",
        "425",
        "SC 13E3",
        "SC 13E3/A",
    }
)
EIGHT_K_ITEM_SECTION = {
    "2.02": "earnings",  # results of operations and financial condition
    "2.01": "mna",  # completion of acquisition or disposition of assets
    "1.01": "contracts_customers",  # entry into a material definitive agreement (type NOT determined)
    "1.02": "contracts_customers",  # termination of a material definitive agreement (type NOT determined)
    "1.03": "legal_regulatory",  # bankruptcy or receivership
    "3.01": "legal_regulatory",  # notice of delisting / failure to satisfy a listing rule
    "4.02": "legal_regulatory",  # non-reliance on previously issued financial statements
    "1.05": "legal_regulatory",  # material cybersecurity incidents
}
ITEM_TITLES = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Continued Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure/Election of Directors or Officers; Compensatory Arrangements",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}
_ITEM_CAVEATS = {
    "contracts_customers": "The 8-K item says an agreement was entered into or ended; it does not say whether it is a "
    "customer contract, financing, partnership or other. Read the filing before characterising it.",
    "mna": "Item 2.01 reports a completed acquisition/disposition; terms and materiality are in the filing.",
    "legal_regulatory": "The item code is a regulatory/legal event category; the filing text holds the facts.",
    "earnings": "Item 2.02 marks an earnings release; figures are in the exhibit, not in this item.",
}


def sections_for_filing(form: str, items: str) -> list[tuple[str, tuple[str, ...]]]:
    """[(section, item codes that put the filing there)] for one filing. A filing can land in several sections. 8-K
    item 9.01 (exhibits) never places a filing by itself; an 8-K whose items are all unmapped lands in ``other``."""
    codes = tuple(c.strip() for c in items.split(",") if c.strip())
    form = form.strip().upper()
    if form in ANNUAL_QUARTERLY_FORMS:
        return [("filings", ())]
    if form in INSIDER_FORMS:
        return [("insiders", ())]
    if form in OWNERSHIP_FORMS:
        return [("ownership", ())]
    if form in MNA_FORMS:
        return [("mna", ())]
    if form in ("8-K", "8-K/A"):
        placed: dict[str, list[str]] = {}
        for code in codes:
            if code != "9.01":
                placed.setdefault(EIGHT_K_ITEM_SECTION.get(code, "other"), []).append(code)
        return [(section, tuple(cs)) for section, cs in placed.items()] or [("other", codes)]
    return []  # any other form type is not part of the packet (the count is reported by the caller)


def filing_url(cik: str, accession: str, document: str | None) -> str:
    folder = ARCHIVE_URL.format(cik_number=int(cik), accession_nodash=accession.replace("-", ""))
    return f"{folder}{document}" if document else folder


def classify_submissions(
    payload: dict[str, Any], company: Company, *, since: date, retrieved_at: datetime, source_url: str
) -> tuple[dict[str, list[EvidenceItem]], dict[str, Any]]:
    """Turn the ``filings.recent`` block of an SEC submissions document into evidence items by section.

    Returns ({section: items}, window) where ``window`` states what the document covered, so "nothing found" is only
    claimed for the dates actually present.
    """
    recent = (payload.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    column = {
        name: recent.get(name) or [""] * len(forms)
        for name in ("accessionNumber", "filingDate", "reportDate", "items", "primaryDocument")
    }
    out: dict[str, list[EvidenceItem]] = {}
    skipped_forms: dict[str, int] = {}
    dates: list[date] = []
    for i, form in enumerate(forms):
        try:
            filed = date.fromisoformat(column["filingDate"][i])
        except _BAD_DATE:
            continue
        dates.append(filed)
        if filed < since:
            continue
        accession = column["accessionNumber"][i]
        placements = sections_for_filing(str(form), str(column["items"][i] or ""))
        if not placements:
            skipped_forms[str(form)] = skipped_forms.get(str(form), 0) + 1
            continue
        report = column["reportDate"][i]
        try:
            period = date.fromisoformat(report) if report else None
        except ValueError:
            period = None
        for section, codes in placements:
            titles = [f"{c} {ITEM_TITLES.get(c, 'item')}" for c in codes]
            content = f"Form {form} filed {filed.isoformat()}" + (f" (items: {'; '.join(titles)})" if titles else "")
            out.setdefault(section, []).append(
                EvidenceItem(
                    id=item_id(section, company.ticker, accession, ",".join(codes)),
                    section=section,
                    company=company,
                    source="sec",
                    source_type="SEC_FILING",
                    as_of=period or filed,
                    filed=filed,
                    content=content,
                    data={"form": form, "items": list(codes), "accession_no": accession, "report_date": period},
                    provenance={
                        "provider": "sec",
                        "dataset": "submissions",
                        "accession_no": accession,
                        "url": filing_url(company.cik or "0", accession, column["primaryDocument"][i] or None),
                        "submissions_url": source_url,
                        "retrieved_at": retrieved_at,
                    },
                    caveats=[_ITEM_CAVEATS[section]] if section in _ITEM_CAVEATS else [],
                )
            )
    for items in out.values():
        items.sort(key=lambda it: (it.filed or date.min, it.id), reverse=True)
    window = {
        "since": since,
        "oldest_filing_in_document": min(dates) if dates else None,
        "newest_filing_in_document": max(dates) if dates else None,
        "document": "SEC submissions filings.recent (roughly the latest 1000 filings or one year, whichever is more)",
        "forms_not_classified": skipped_forms,
    }
    return out, window
