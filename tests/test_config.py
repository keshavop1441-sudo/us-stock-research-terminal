from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings, ThinkingMode

ROOT = Path(__file__).resolve().parent.parent


def test_defaults(clean_env):
    settings = Settings.from_env(None)
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.ollama_model == "qwen3.5:4b"
    assert settings.fast_think is False
    assert settings.deep_think is True
    assert settings.thinking_mode is ThinkingMode.DEEP
    assert settings.resolved_database_path == PROJECT_ROOT / "data" / "research.duckdb"


def test_environment_overrides(clean_env):
    clean_env.setenv("OLLAMA_BASE_URL", "http://example:1234/")
    clean_env.setenv("OLLAMA_MODEL", "other:7b")
    clean_env.setenv("FAST_THINK", "true")
    settings = Settings.from_env(None)
    assert settings.ollama_base_url == "http://example:1234"  # trailing slash removed
    assert settings.ollama_model == "other:7b"
    assert settings.thinking_mode is ThinkingMode.FAST  # FAST_THINK wins over DEEP_THINK


def test_empty_values_are_treated_as_unset(clean_env):
    clean_env.setenv("OLLAMA_MODEL", "  ")
    assert Settings.from_env(None).ollama_model == "qwen3.5:4b"


@pytest.mark.parametrize(
    ("fast", "deep", "expected"),
    [(False, True, ThinkingMode.DEEP), (True, False, ThinkingMode.FAST), (False, False, ThinkingMode.OFF)],
)
def test_thinking_mode(fast, deep, expected):
    assert Settings(fast_think=fast, deep_think=deep).thinking_mode is expected


def test_invalid_values_are_rejected(clean_env):
    clean_env.setenv("FAST_THINK", "maybe")
    with pytest.raises(ValidationError):
        Settings.from_env(None)
    clean_env.setenv("FAST_THINK", "false")
    clean_env.setenv("OLLAMA_BASE_URL", "localhost:11434")
    with pytest.raises(ValidationError):
        Settings.from_env(None)


def test_database_path_resolution(tmp_path):
    assert Settings(database_path=Path("data/x.duckdb")).resolved_database_path == PROJECT_ROOT / "data" / "x.duckdb"
    absolute = tmp_path / "x.duckdb"
    assert Settings(database_path=absolute).resolved_database_path == absolute


def test_env_file_is_loaded_but_environment_wins(clean_env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OLLAMA_MODEL=from-file\nOLLAMA_BASE_URL=http://file:1\n")
    clean_env.setenv("OLLAMA_BASE_URL", "http://env:2")
    settings = Settings.from_env(env_file)
    assert settings.ollama_model == "from-file"
    assert settings.ollama_base_url == "http://env:2"


def test_env_example_matches_defaults():
    values = dotenv_values(ROOT / ".env.example")
    defaults = Settings()
    assert values["OLLAMA_BASE_URL"] == defaults.ollama_base_url
    assert values["OLLAMA_MODEL"] == defaults.ollama_model
    assert values["FAST_THINK"] == "false"
    assert values["DEEP_THINK"] == "true"


def test_secrets_and_local_data_are_git_ignored():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored
    assert "*.duckdb" in ignored
    assert "!.env.example" in ignored
