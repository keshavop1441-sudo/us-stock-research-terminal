import pytest

from app.config import _ENV_VARS
from app.database.connection import connect, init_database


@pytest.fixture
def clean_env(monkeypatch):
    """No configuration variables set, and any set later by load_dotenv are removed at teardown."""
    for var in _ENV_VARS.values():
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)
    return monkeypatch


@pytest.fixture
def app_env(clean_env, tmp_path):
    """Hermetic environment for app tests: temp database, Ollama pointed at a closed port."""
    db_path = tmp_path / "research.duckdb"
    clean_env.setenv("DATABASE_PATH", str(db_path))
    clean_env.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")  # discard port: connection refused
    return db_path


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "research.duckdb"
    init_database(path)
    return path


@pytest.fixture
def con(db_path):
    with connect(db_path) as connection:
        yield connection
