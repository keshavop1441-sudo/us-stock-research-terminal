"""One retrieval from one provider: the unit of provenance. Fetching produces these; nothing else writes ``sources``."""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.models.records import SourceRecord


def utc_now() -> datetime:
    """Naive UTC, the project's timestamp convention."""
    return datetime.now(UTC).replace(tzinfo=None)


def content_hash(payload: object) -> str:
    """sha256 of the canonical JSON of a payload (stable across key order and across SEC/OpenBB origins)."""
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class Retrieval:
    provider: str  # 'sec', 'cboe', 'nasdaq'
    dataset: str  # 'companyfacts', 'submissions', 'company_tickers_exchange', 'equity.historical', ...
    command: str  # 'HTTP GET <url>' or 'obb.cboe.equity.historical'
    parameters: dict[str, object]
    payload: object  # parsed JSON (SEC) or list of row dicts (OpenBB)
    retrieved_at: datetime
    url: str | None = None
    provider_version: str | None = None
    as_of: datetime | None = None  # the provider's own timestamp of the data, when it states one
    is_fallback: bool = False
    raw_path: str | None = None  # where the verbatim payload was saved (RawStore), when it was
    request_count: int = 1
    notes: list[str] = field(default_factory=list)

    @property
    def hash(self) -> str:
        return content_hash(self.payload)

    def to_source_record(self) -> SourceRecord:
        detail = {"raw_path": self.raw_path, "notes": self.notes} if (self.raw_path or self.notes) else None
        return SourceRecord(
            provider=self.provider,
            dataset=self.dataset,
            url=self.url,
            content_hash=self.hash,
            detail=json.dumps(detail, sort_keys=True) if detail else None,
            command=self.command,
            parameters=json.dumps(self.parameters, sort_keys=True, default=str),
            provider_version=self.provider_version,
            as_of=self.as_of,
            is_fallback=self.is_fallback,
        )
