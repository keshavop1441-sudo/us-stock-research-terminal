"""Settings for the deterministic research engine, from environment variables and an optional .env file."""

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_DATABASE_PATH = Path("data/research.duckdb")

# Settings field -> environment variable.
_ENV_VARS = {
    "database_path": "DATABASE_PATH",
    "sec_user_agent": "SEC_USER_AGENT",
}


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    database_path: Path = DEFAULT_DATABASE_PATH
    # SEC fair-access policy: requests must identify the caller. Never hard-coded: the operator supplies it.
    # Format: "<ApplicationName> <contact>", e.g. "MyResearchApp operator@example.org" (contact = email or URL).
    sec_user_agent: str | None = None

    @field_validator("sec_user_agent")
    @classmethod
    def _check_sec_user_agent(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = " ".join(value.split())
        name, _, contact = value.partition(" ")
        if not name or not contact or not ("@" in contact or contact.startswith(("http://", "https://"))):
            raise ValueError("SEC_USER_AGENT must be '<ApplicationName> <contact email or URL>'")
        return value

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
