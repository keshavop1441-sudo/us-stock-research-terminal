"""Application settings, loaded from environment variables and an optional .env file."""

import os
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_DATABASE_PATH = Path("data/research.duckdb")

# Settings field -> environment variable.
_ENV_VARS = {
    "ollama_base_url": "OLLAMA_BASE_URL",
    "ollama_model": "OLLAMA_MODEL",
    "fast_think": "FAST_THINK",
    "deep_think": "DEEP_THINK",
    "database_path": "DATABASE_PATH",
}


class ThinkingMode(StrEnum):
    FAST = "fast"
    DEEP = "deep"
    OFF = "off"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3.5:4b"
    fast_think: bool = False
    deep_think: bool = True
    database_path: Path = DEFAULT_DATABASE_PATH

    @field_validator("ollama_base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("OLLAMA_BASE_URL must start with http:// or https://")
        return value

    @property
    def thinking_mode(self) -> ThinkingMode:
        """FAST_THINK takes precedence when both flags are set."""
        if self.fast_think:
            return ThinkingMode.FAST
        if self.deep_think:
            return ThinkingMode.DEEP
        return ThinkingMode.OFF

    @property
    def resolved_database_path(self) -> Path:
        """Absolute database path; relative values are anchored at the project root."""
        path = self.database_path.expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path

    @classmethod
    def from_env(cls, env_file: Path | None = DEFAULT_ENV_FILE) -> "Settings":
        """Build settings from the process environment.

        Values already present in the environment win over the .env file.
        Empty values are treated as unset. Pass ``env_file=None`` to skip .env.
        """
        if env_file is not None and env_file.is_file():
            load_dotenv(env_file, override=False)
        values = {field: os.environ[var].strip() for field, var in _ENV_VARS.items() if os.environ.get(var, "").strip()}
        return cls(**values)
