"""Watchlists."""

import polars as pl

from app.services.db import read_access, write_access


def list_watchlists() -> pl.DataFrame:
    with read_access() as repository:
        return repository.list_watchlists()


def create_watchlist(name: str, description: str | None = None) -> int:
    """Create a watchlist. Raises ValueError for an empty or duplicate name."""
    with write_access("create watchlist") as repository:
        return repository.create_watchlist(name, description)
