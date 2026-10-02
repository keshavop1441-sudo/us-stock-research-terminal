"""Verbatim copies of what providers returned, so rows can be re-derived and audited without re-fetching.

Files are gzip-compressed JSON under ``<root>/<run_id>/``; the path is recorded in ``sources.detail``. The directory
lives beside the (git-ignored) database. Writing a raw copy never replaces provenance in the database: the hash in
``sources`` is computed from the same payload.
"""

import gzip
import json
import re
from pathlib import Path

from app.ingestion.retrieval import Retrieval

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class RawStore:
    def __init__(self, root: Path, run_id: str):
        self._dir = root / run_id
        self._dir.mkdir(parents=True, exist_ok=True)

    def save(self, retrieval: Retrieval, label: str) -> Path:
        name = _UNSAFE.sub("_", f"{retrieval.provider}-{retrieval.dataset}-{label}") + ".json.gz"
        path = self._dir / name
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            json.dump(retrieval.payload, handle, sort_keys=True, default=str)
        retrieval.raw_path = str(path)
        return path
