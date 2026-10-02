"""Configuration for the research engine."""

from pydantic import ValidationError

from app.config import Settings
from app.services.errors import ConfigurationError


def get_settings() -> Settings:
    """Current settings from the environment/.env. Raises ``ConfigurationError`` if invalid."""
    try:
        return Settings.from_env()
    except ValidationError as exc:
        raise ConfigurationError(str(exc)) from exc
