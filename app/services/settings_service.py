"""Configuration and provider status for the UI."""

from pydantic import ValidationError

from app.agent.llm_provider import get_provider
from app.config import DEFAULT_ENV_FILE, Settings
from app.models.status import ProviderStatus
from app.services.errors import ConfigurationError


def get_settings() -> Settings:
    """Current settings from the environment/.env. Raises ``ConfigurationError`` if invalid."""
    try:
        return Settings.from_env()
    except ValidationError as exc:
        raise ConfigurationError(str(exc)) from exc


def env_file_found() -> bool:
    return DEFAULT_ENV_FILE.is_file()


def env_file_path() -> str:
    return str(DEFAULT_ENV_FILE)


def get_ollama_status(settings: Settings) -> ProviderStatus:
    return get_provider(settings).check_status()
