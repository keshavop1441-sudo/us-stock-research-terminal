"""Data structures of one ingestion run (what the P0 report is built from). Plain data, JSON-serialisable via
``to_dict``."""

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path


@dataclass
class TableCounts:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates: int = 0

    def add(self, result) -> None:  # a ``WriteRepository`` UpsertResult (not imported: layering)
        self.inserted += result.inserted
        self.updated += result.updated
        self.unchanged += result.unchanged
        self.duplicates += result.duplicates


@dataclass
class Issue:
    """A failure that was caught and recorded. Nothing that fails is dropped without one of these."""

    stage: str
    symbol: str | None
    kind: str  # stable class, e.g. HTTP_403, IDENTITY, MALFORMED_RESPONSE, NO_ALLOWLISTED_FACTS
    message: str


@dataclass
class StageResult:
    name: str
    status: str = "NOT_RUN"  # OK | PARTIAL | FAILED | SKIPPED | NOT_RUN
    started_at: datetime | None = None
    ended_at: datetime | None = None
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0

    @property
    def seconds(self) -> float | None:
        return (self.ended_at - self.started_at).total_seconds() if self.started_at and self.ended_at else None


@dataclass
class SymbolOutcome:
    symbol: str
    cik: str | None = None
    identity: bool = False
    price: bool = False
    quote: bool = False
    facts: bool = False
    price_rows: int = 0
    price_first: date | None = None
    price_last: date | None = None
    price_gaps: list[tuple[date, date, int]] = field(default_factory=list)
    fact_rows: int = 0
    used_fallback_prices: bool = False
    seconds: float = 0.0


@dataclass
class RunReport:
    run_id: str
    mode: str  # LIVE | SIMULATED
    manifest_digest: str
    symbols: list[str]
    as_of: date
    started_at: datetime | None = None
    ended_at: datetime | None = None
    fatal: str | None = None  # why the run could not proceed at all
    stages: dict[str, StageResult] = field(default_factory=dict)
    outcomes: dict[str, SymbolOutcome] = field(default_factory=dict)
    tables: dict[str, TableCounts] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    warnings: list[dict[str, object]] = field(default_factory=list)  # {category, symbol, message}
    retrievals_recorded: int = 0  # rows appended to ``sources`` by this run
    fallback_retrievals: int = 0
    requests: dict[str, object] = field(default_factory=dict)
    multi_class: dict[str, list[str]] = field(default_factory=dict)  # cik -> listings of one issuer
    split_rebases: dict[str, float] = field(default_factory=dict)
    facts_skipped: dict[str, int] = field(default_factory=dict)
    db_size_bytes: int | None = None

    def counts(self, table: str) -> TableCounts:
        return self.tables.setdefault(table, TableCounts())

    def issue(self, stage: str, symbol: str | None, kind: str, message: str) -> None:
        self.issues.append(Issue(stage, symbol, kind, message))

    def warn(self, category: str, symbol: str | None, message: str) -> None:
        self.warnings.append({"category": category, "symbol": symbol, "message": message})

    @property
    def runtime_seconds(self) -> float | None:
        return (self.ended_at - self.started_at).total_seconds() if self.started_at and self.ended_at else None

    def totals(self) -> TableCounts:
        total = TableCounts()
        for counts in self.tables.values():
            total.inserted += counts.inserted
            total.updated += counts.updated
            total.unchanged += counts.unchanged
            total.duplicates += counts.duplicates
        return total

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["runtime_seconds"] = self.runtime_seconds
        data["totals"] = asdict(self.totals())
        for name, stage in self.stages.items():
            data["stages"][name]["seconds"] = stage.seconds
        return data


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
