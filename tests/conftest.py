import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pytest

from app.config import _ENV_VARS
from app.database.connection import ensure_database

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent


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
    """A freshly initialised (empty, current-schema) database file."""
    path = tmp_path / "research.duckdb"
    ensure_database(path)
    return path


@pytest.fixture
def con(db_path):
    """Raw DuckDB connection for test setup and assertions about stored data (tests only)."""
    connection = duckdb.connect(str(db_path))
    yield connection
    connection.close()


@pytest.fixture
def hold(db_path):
    """Run ``tests/hold_database.py`` in ANOTHER process and keep it holding a resource until teardown."""
    procs: list[subprocess.Popen] = []

    @contextmanager
    def holder(mode: str, path: Path | None = None):
        proc = subprocess.Popen(
            [sys.executable, str(TESTS_DIR / "hold_database.py"), str(path or db_path), mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        procs.append(proc)
        line = proc.stdout.readline().strip()
        assert line == "READY", f"holder failed: {line!r} {proc.stderr.read() if proc.poll() is not None else ''}"
        try:
            yield proc
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    yield holder
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
