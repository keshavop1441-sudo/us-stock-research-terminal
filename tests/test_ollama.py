import httpx
import pytest

from app.agent.llm_provider import LLMProvider, OllamaProvider, get_provider
from app.config import Settings

BASE = "http://ollama.test:11434"


def provider(handler, model="qwen3.5:4b") -> OllamaProvider:
    return OllamaProvider(BASE, model, transport=httpx.MockTransport(handler))


def tags(*names):
    return lambda request: httpx.Response(200, json={"models": [{"name": n} for n in names]})


def test_available_with_model_installed():
    status = provider(tags("qwen3.5:4b", "llama3:8b")).check_status()
    assert status.available is True
    assert status.model_installed is True
    assert status.installed_models == ["qwen3.5:4b", "llama3:8b"]
    assert status.configured_model == "qwen3.5:4b"


def test_available_but_model_missing():
    status = provider(tags("llama3:8b")).check_status()
    assert status.available is True
    assert status.model_installed is False
    assert "ollama pull qwen3.5:4b" in status.detail


def test_untagged_model_matches_latest():
    assert provider(tags("qwen3.5:latest"), model="qwen3.5").check_status().model_installed is True


def test_requests_the_tags_endpoint():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"models": []})

    provider(handler).check_status()
    assert seen == [f"{BASE}/api/tags"]


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(500),
        lambda request: httpx.Response(200, text="not json"),
        lambda request: httpx.Response(200, json=["unexpected"]),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")),
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
    ],
    ids=["http-500", "not-json", "wrong-shape", "connect-error", "timeout"],
)
def test_failures_become_status_not_exceptions(handler):
    status = provider(handler).check_status()
    assert status.available is False
    assert status.model_installed is None
    assert status.installed_models == []
    assert status.detail


def test_real_unreachable_server_does_not_raise():
    status = OllamaProvider("http://127.0.0.1:9", "qwen3.5:4b", timeout=1).check_status()
    assert status.available is False


def test_malformed_url_does_not_raise():
    assert OllamaProvider("http://", "m", timeout=1).check_status().available is False


def test_generate_is_a_placeholder():
    with pytest.raises(NotImplementedError):
        provider(tags()).generate("hello")


def test_factory_uses_settings():
    built = get_provider(Settings(ollama_base_url="http://host:1/", ollama_model="m"))
    assert isinstance(built, LLMProvider)
    assert (built.base_url, built.model) == ("http://host:1", "m")
