"""Natural-language requests typed into the terminal (storage only; interpretation comes later)."""

import polars as pl

from app.database.write_repository import QUERY_TEXT_MAX_LENGTH
from app.services.db import read_access, write_access

__all__ = ["QUERY_TEXT_MAX_LENGTH", "recent_queries", "submit_query"]


def submit_query(text: str) -> int:
    """Save a request and return its id. Raises ValueError for empty/oversized text."""
    with write_access("save request") as repository:
        return repository.record_query(text)


def recent_queries(limit: int = 10) -> pl.DataFrame:
    with read_access() as repository:
        return repository.recent_queries(limit)
