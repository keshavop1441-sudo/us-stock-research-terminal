"""Canonical identifiers. The ONLY place CIKs and tickers are normalised.

Every layer that stores or looks up an issuer or security (record models, repositories,
services) calls these functions; nobody re-implements the rules.
"""

import re

CIK_LENGTH = 10
_CIK_DIGITS = re.compile(r"[0-9]+")
_CIK_PREFIX = re.compile(r"^CIK[\s:#_-]*", re.IGNORECASE)
# Letters/digits plus the separators US tickers use (BRK.B, BRK-B, BRK/B).
_TICKER = re.compile(r"[A-Z0-9][A-Z0-9./-]{0,14}")


class InvalidCikError(ValueError):
    """A value cannot be turned into a valid CIK."""


class InvalidTickerError(ValueError):
    """A value cannot be turned into a valid ticker."""


def normalize_cik(value: int | str) -> str:
    """Return the canonical CIK: a zero-padded 10-digit string, e.g. 320193 -> '0000320193'.

    Accepts non-negative integers and strings of ASCII digits. Surrounding whitespace and an
    EDGAR-style ``CIK`` prefix are tolerated. Extra leading zeros are tolerated as long as the
    numeric value fits in 10 digits. Everything else (floats, booleans, signs, separators,
    empty strings, zero, values above 9,999,999,999) raises ``InvalidCikError``.
    """
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise InvalidCikError(f"CIK must be an int or str, got {type(value).__name__}: {value!r}")
    if isinstance(value, str):
        text = _CIK_PREFIX.sub("", value.strip())
        if not _CIK_DIGITS.fullmatch(text):
            raise InvalidCikError(f"CIK must contain only digits, got {value!r}")
        number = int(text)
    else:
        number = value
    if number <= 0:
        raise InvalidCikError(f"CIK must be a positive number, got {value!r}")
    if number >= 10**CIK_LENGTH:
        raise InvalidCikError(f"CIK must have at most {CIK_LENGTH} digits, got {value!r}")
    return f"{number:0{CIK_LENGTH}d}"


def normalize_optional_cik(value: int | str | None) -> str | None:
    """Like ``normalize_cik`` but ``None`` (unknown) stays ``None``. Empty strings are still invalid."""
    return None if value is None else normalize_cik(value)


def normalize_ticker(value: str) -> str:
    """Return the canonical ticker: stripped and upper-cased. Raises ``InvalidTickerError`` otherwise.

    Different sources spell share-class tickers differently (BRK.B / BRK-B / BRK/B). Reconciling
    those spellings is a data-source concern and is deliberately not guessed at here.
    """
    if not isinstance(value, str):
        raise InvalidTickerError(f"Ticker must be a string, got {type(value).__name__}: {value!r}")
    ticker = value.strip().upper()
    if not _TICKER.fullmatch(ticker):
        raise InvalidTickerError(f"Not a valid ticker: {value!r}")
    return ticker


def looks_like_cik(value: str) -> bool:
    """True if ``value`` can only be a CIK (all digits, optionally with a ``CIK`` prefix)."""
    return bool(_CIK_DIGITS.fullmatch(_CIK_PREFIX.sub("", value.strip())))
