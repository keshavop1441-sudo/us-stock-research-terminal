"""LLM provider abstraction.

Security boundary: a provider only turns text into text. It is never handed a database
connection, a shell, a Python interpreter or arbitrary SQL. When the agent gains tool use,
the model may only request tools registered in ``app.tools.registry`` with validated arguments.
"""

from abc import ABC, abstractmethod

import httpx

from app.config import Settings
from app.models.status import ProviderStatus

OLLAMA_STATUS_TIMEOUT_SECONDS = 2.0
# Named tuples: see the note in app/database/locking.py about unparenthesised `except` clauses.
_UNREACHABLE = (httpx.HTTPError, httpx.InvalidURL)
_BAD_RESPONSE = (ValueError, AttributeError)


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def check_status(self) -> ProviderStatus:
        """Report availability. Must never raise: an absent provider is a status, not an error."""

    @abstractmethod
    def generate(self, prompt: str, *, system: str | None = None) -> str:
        """Return the model's text completion for ``prompt``."""


class OllamaProvider(LLMProvider):
    """Placeholder for a local Ollama server: availability detection only, no generation yet."""

    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = OLLAMA_STATUS_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._timeout = timeout
        self._transport = transport  # injectable for tests

    def check_status(self) -> ProviderStatus:
        def status(available: bool, detail: str, **extra) -> ProviderStatus:
            return ProviderStatus(
                name=self.name,
                base_url=self.base_url,
                available=available,
                detail=detail,
                configured_model=self.model,
                **extra,
            )

        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.get(f"{self.base_url}/api/tags")
            response.raise_for_status()
            payload = response.json()
            installed = [
                entry.get("name") or entry.get("model")
                for entry in payload.get("models", [])
                if entry.get("name") or entry.get("model")
            ]
        except httpx.TimeoutException:
            return status(False, f"Timed out after {self._timeout:g}s contacting {self.base_url}.")
        except httpx.HTTPStatusError as exc:
            return status(False, f"Server answered HTTP {exc.response.status_code}.")
        except _UNREACHABLE:
            return status(False, f"Cannot connect to {self.base_url}. Is Ollama running?")
        except _BAD_RESPONSE:
            return status(False, f"{self.base_url} did not return a valid Ollama response.")

        has_model = any(_model_matches(self.model, name) for name in installed)
        detail = (
            "Ollama is running."
            if has_model
            else f"Ollama is running, but model '{self.model}' is not installed (ollama pull {self.model})."
        )
        return status(True, detail, model_installed=has_model, installed_models=installed)

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        raise NotImplementedError("OllamaProvider.generate is a placeholder; generation is not wired up yet.")


def _model_matches(configured: str, installed: str) -> bool:
    """'qwen3.5:4b' matches itself; an untagged name matches its ':latest' tag."""
    if configured == installed:
        return True
    return ":" not in configured and installed == f"{configured}:latest"


def get_provider(settings: Settings) -> LLMProvider:
    return OllamaProvider(settings.ollama_base_url, settings.ollama_model)
