"""Plain data models describing the health of the engine's components."""

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


class PackageStatus(BaseModel):
    name: str
    version: str | None = None  # None = not installed

    @property
    def installed(self) -> bool:
        return self.version is not None


class OpenBBStatus(BaseModel):
    packages: list[PackageStatus]
    runtime_checked: bool = False
    runtime_ok: bool | None = None  # None = not checked
    providers: list[str] = Field(default_factory=list)
    routers: list[str] = Field(default_factory=list)
    detail: str = ""

    @property
    def all_installed(self) -> bool:
        return all(p.installed for p in self.packages)


class DatabaseStatus(BaseModel):
    path: Path
    exists: bool
    initialized: bool = False
    schema_version: int | None = None
    size_bytes: int | None = None
    error: str | None = None


class RefreshStatus(BaseModel):
    """Is another process currently writing to the database (e.g. a data refresh)?"""

    active: bool = False
    operation: str | None = None
    pid: int | None = None
    started_at: datetime | None = None
