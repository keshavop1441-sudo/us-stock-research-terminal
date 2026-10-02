from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from app.config import PROJECT_ROOT, Settings

ROOT = Path(__file__).resolve().parent.parent


def test_defaults(clean_env):
    settings = Settings.from_env(None)
    assert settings.sec_user_agent is None
    assert settings.resolved_database_path == PROJECT_ROOT / "data" / "research.duckdb"


def test_there_is_no_local_llm_configuration():
    assert set(Settings.model_fields) == {"database_path", "sec_user_agent"}


def test_database_path_resolution(tmp_path):
    assert Settings(database_path=Path("data/x.duckdb")).resolved_database_path == PROJECT_ROOT / "data" / "x.duckdb"
    absolute = tmp_path / "x.duckdb"
    assert Settings(database_path=absolute).resolved_database_path == absolute


def test_env_file_is_loaded_but_environment_wins(clean_env, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_PATH=from-file.duckdb\nSEC_USER_AGENT=FileApp file@example.org\n")
    clean_env.setenv("SEC_USER_AGENT", "EnvApp env@example.org")
    settings = Settings.from_env(env_file)
    assert settings.database_path == Path("from-file.duckdb")
    assert settings.sec_user_agent == "EnvApp env@example.org"


def test_empty_values_are_treated_as_unset(clean_env):
    clean_env.setenv("SEC_USER_AGENT", "   ")
    assert Settings.from_env(None).sec_user_agent is None


def test_env_example_documents_only_the_supported_variables():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SEC_USER_AGENT" in text and "DATABASE_PATH" in text
    assert not any(word in text.lower() for word in ("ollama", "qwen", "deepseek", "think"))
    assert set(dotenv_values(ROOT / ".env.example")) <= {"DATABASE_PATH", "SEC_USER_AGENT"}


def test_secrets_and_local_data_are_git_ignored():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored
    assert "*.duckdb" in ignored
    assert "!.env.example" in ignored
    assert "research_data/" in ignored and "dist/" in ignored


# --- SEC_USER_AGENT ---------------------------------------------------------------------------------------------------
def test_sec_user_agent_is_optional_and_never_defaulted_to_a_contact():
    assert Settings().sec_user_agent is None
    assert Settings(sec_user_agent="   ").sec_user_agent is None


def test_sec_user_agent_requires_application_name_and_contact(monkeypatch):
    assert Settings(sec_user_agent="  MyTerminal   ops@example.org ").sec_user_agent == "MyTerminal ops@example.org"
    assert Settings(sec_user_agent="MyTerminal https://example.org/contact").sec_user_agent
    for bad in ("MyTerminal", "ops@example.org", "MyTerminal not-a-contact"):
        with pytest.raises(ValueError, match="SEC_USER_AGENT"):
            Settings(sec_user_agent=bad)
    monkeypatch.setenv("SEC_USER_AGENT", "MyTerminal ops@example.org")
    assert Settings.from_env(env_file=None).sec_user_agent == "MyTerminal ops@example.org"
    monkeypatch.delenv("SEC_USER_AGENT")
    assert Settings.from_env(env_file=None).sec_user_agent is None


def test_invalid_sec_user_agent_in_the_environment_is_rejected(clean_env):
    clean_env.setenv("SEC_USER_AGENT", "justaname")
    with pytest.raises(ValidationError):
        Settings.from_env(None)


def test_repository_files_contain_no_personal_contact_for_the_sec():
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SEC_USER_AGENT=<ApplicationName>" in text and "@" not in text.split("SEC_USER_AGENT")[1].splitlines()[0]
