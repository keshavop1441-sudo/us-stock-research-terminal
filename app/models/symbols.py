"""Share-class ticker spellings across providers (audit: LIVE, 2026-10-02).

SEC (company_tickers, submissions, ``obb.sec.cik_map``) spells Berkshire's B shares ``BRK-B``; Nasdaq and Cboe use
``BRK.B`` (Nasdaq's ``BRK/B`` and Cboe's ``BRK-B`` fail: EmptyDataError / HTTP 403). The issuer's identity is the CIK,
so a ticker spelling is only a lookup key. The internal form is the SEC's: the SEC list is the identity authority.

Only the single-letter class suffix on a dot/slash/dash separator is rewritten. Preferred shares, units and warrants
use other conventions that were NOT verified, and are passed through unchanged rather than guessed at.
"""

import re

from app.models.identifiers import normalize_ticker

PROVIDER_SEPARATOR = {"sec": "-", "nasdaq": ".", "cboe": "."}
_CLASS_SUFFIX = re.compile(r"^(?P<root>[A-Z0-9]{1,6})[./-](?P<cls>[A-Z])$")


class UnknownProviderError(ValueError):
    """No symbol convention is recorded for this provider."""


def canonical_symbol(value: str) -> str:
    """Internal spelling: upper case, class suffix separated by '-' (BRK.B / BRK/B / brk-b -> BRK-B)."""
    ticker = normalize_ticker(value)
    match = _CLASS_SUFFIX.fullmatch(ticker)
    return f"{match['root']}-{match['cls']}" if match else ticker


def provider_symbol(value: str, provider: str) -> str:
    """The spelling ``provider`` accepts for the same security (BRK-B -> BRK.B for nasdaq/cboe)."""
    try:
        separator = PROVIDER_SEPARATOR[provider.strip().lower()]
    except KeyError:
        raise UnknownProviderError(f"no symbol convention recorded for provider {provider!r}") from None
    canonical = canonical_symbol(value)
    match = _CLASS_SUFFIX.fullmatch(canonical)
    return f"{match['root']}{separator}{match['cls']}" if match else canonical
