"""Company lookup for the Company page."""

import polars as pl

from app.models.identifiers import InvalidCikError, InvalidTickerError, looks_like_cik, normalize_cik
from app.services.db import read_access

__all__ = ["InvalidCikError", "InvalidTickerError", "find_companies", "price_history"]


def find_companies(query: str) -> pl.DataFrame:
    """Look up securities by CIK (all digits, optionally 'CIK'-prefixed) or by ticker.

    Raises ``InvalidCikError``/``InvalidTickerError`` (both ValueError) for malformed input.
    """
    with read_access() as repository:
        if looks_like_cik(query):
            return repository.find_securities_by_cik(normalize_cik(query))
        return repository.find_securities_by_ticker(query)


def price_history(security_id: int) -> pl.DataFrame:
    with read_access() as repository:
        return repository.price_history(security_id)
