"""Plain data models describing the health of the terminal's components."""

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field


class ProviderStatus(BaseModel):
    """Availability of an LLM provider (e.g. a local Ollama server)."""

    name: str
    base_url: str | None = None
    available: bool
    detail: str
    configured_model: str | None = None
    model_installed: bool | None = None  # None = unknown (provider unreachable)
    installed_models: list[str] = Field(default_factory=list)


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


class DataStatus(BaseModel):
    database: DatabaseStatus
    counts: dict[str, int | None] = Field(default_factory=dict)  # None = unavailable
    last_sync: datetime | None = None  # None = never synchronized
    openbb: OpenBBStatus
    ollama: ProviderStatus
