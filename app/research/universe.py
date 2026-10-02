"""Which symbols a research command may ingest. Pure.

The audited P0 manifest (14 securities) keeps its roles, audited CIKs and primary-listing designations. Any other
symbol becomes an ad-hoc entry with NO audited expectations: its CIK is whatever the SEC ticker map says (a symbol that
is absent or ambiguous fails identity; nothing is guessed).

The full market universe is NOT loaded (docs/data_coverage.yaml ``universe_pilot``: do not do that until the staged
acceptance criteria pass). Ingestion is therefore bounded per call, and screens run on what has been ingested.
"""

from collections.abc import Iterable

from app.ingestion.manifest import MANIFEST, P0Security
from app.models.identifiers import normalize_ticker
from app.models.symbols import canonical_symbol

MAX_INGEST_SYMBOLS = 25  # per call: keeps one run well inside SEC/OpenBB politeness limits and a tool-call timeout
STAGE_ALIASES = {"identity": "identity", "prices": "prices", "quotes": "quotes", "facts": "sec_facts"}
DEFAULT_STAGES = ("identity", "prices", "quotes", "sec_facts")


class UniverseError(ValueError):
    """The requested symbols or stages are unusable."""


def parse_symbols(values: Iterable[str], *, limit: int | None = None) -> tuple[str, ...]:
    """Canonical, de-duplicated, order-preserving. Raises ``UniverseError`` for an empty/invalid/too long list."""
    out: dict[str, None] = {}
    for raw in values:
        for part in str(raw).replace(";", ",").split(","):
            if part.strip():
                try:
                    out[canonical_symbol(normalize_ticker(part))] = None
                except ValueError as exc:
                    raise UniverseError(f"invalid ticker {part.strip()!r}: {exc}") from exc
    if not out:
        raise UniverseError("no symbols given")
    if limit is not None and len(out) > limit:
        raise UniverseError(f"{len(out)} symbols requested; at most {limit} per call (split the request)")
    return tuple(out)


def parse_stages(names: Iterable[str] | None) -> tuple[str, ...]:
    """Stage names as users write them (identity, prices, quotes, facts); identity always runs first."""
    if not names:
        return DEFAULT_STAGES
    chosen = {"identity"}
    for name in names:
        try:
            chosen.add(STAGE_ALIASES[name.strip().lower()])
        except KeyError:
            raise UniverseError(f"unknown stage {name!r}; choose from {sorted(STAGE_ALIASES)}") from None
    return tuple(stage for stage in DEFAULT_STAGES if stage in chosen)


def build_manifest(symbols: Iterable[str]) -> tuple[P0Security, ...]:
    known = {s.symbol: s for s in MANIFEST}
    entries = []
    for symbol in parse_symbols(symbols, limit=MAX_INGEST_SYMBOLS):
        entries.append(
            known.get(symbol)
            or P0Security(symbol, issuer=symbol, roles=("adhoc",), why="requested by the user (not in the audited set)")
        )
    return tuple(entries)
