"""scripts/init_db.py: exit codes and single-writer behaviour."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(f"script_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


init_db = load("init_db")


def test_init_db_creates_the_database_and_is_repeatable(app_env, capsys):
    assert init_db.main() == 0
    assert init_db.main() == 0
    assert app_env.is_file() and "schema v3" in capsys.readouterr().out


def test_init_db_reports_busy_with_its_own_exit_code(app_env, hold, capsys):
    import duckdb

    with hold("lock-only"):
        app_env.unlink()  # (the hold fixture initialised the database) -> now a file WITHOUT a schema,
        duckdb.connect(str(app_env)).close()  # so initialising it needs the writer lock
        assert init_db.main() == 2
    assert "busy" in capsys.readouterr().out
