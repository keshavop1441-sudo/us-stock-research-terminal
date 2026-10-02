"""scripts/init_db.py and scripts/update_data.py: exit codes and single-writer behaviour."""

import importlib.util
from pathlib import Path

import pytest

from app.database import access
from app.database.connection import ensure_database
from app.database.locking import writer_status
from app.models.status import OpenBBStatus, PackageStatus

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    spec = importlib.util.spec_from_file_location(f"script_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


init_db = load("init_db")
update_data = load("update_data")


def fake_openbb(ok: bool):
    return OpenBBStatus(
        packages=[PackageStatus(name="openbb-core", version="2.0.1")],
        runtime_checked=True,
        runtime_ok=ok,
        providers=["sec"],
        detail="fake",
    )


def test_init_db_creates_the_database_and_is_repeatable(app_env, capsys):
    assert init_db.main() == 0
    assert init_db.main() == 0
    assert app_env.is_file() and "schema v2" in capsys.readouterr().out


def test_init_db_reports_busy_with_its_own_exit_code(app_env, hold, capsys):
    import duckdb

    with hold("lock-only"):
        app_env.unlink()  # (the hold fixture initialised the database) -> now a file WITHOUT a schema,
        duckdb.connect(str(app_env)).close()  # so initialising it needs the writer lock
        assert init_db.main() == 2
    assert "busy" in capsys.readouterr().out


def test_update_data_runs_under_the_writer_lock_and_releases_it(app_env, monkeypatch, capsys):
    ensure_database(app_env)
    monkeypatch.setattr(update_data, "runtime_check", lambda: fake_openbb(True))
    seen = []
    real_writer = access.writer

    def spying_writer(*args, **kwargs):
        seen.append(writer_status(app_env).active)  # not yet taken
        return real_writer(*args, **kwargs)

    monkeypatch.setattr(update_data.access, "writer", spying_writer)
    assert update_data.main() == 0
    assert seen == [False]
    out = capsys.readouterr().out
    assert "Write lock acquired" in out and "nothing was downloaded" in out
    assert writer_status(app_env).active is False


def test_a_second_refresh_is_refused_with_exit_code_2_before_doing_any_work(app_env, hold, monkeypatch, capsys):
    ensure_database(app_env)
    monkeypatch.setattr(update_data, "runtime_check", lambda: pytest.fail("must not even check OpenBB"))
    with hold("lock-only"):
        assert update_data.main() == update_data.EXIT_BUSY == 2
    assert "already running" in capsys.readouterr().out


def test_a_refresh_is_refused_while_another_process_owns_the_database_file(app_env, hold, monkeypatch):
    ensure_database(app_env)
    monkeypatch.setattr(update_data, "runtime_check", lambda: fake_openbb(True))
    with hold("duckdb-only"):  # no writer lock, but DuckDB will not let a second process in
        monkeypatch.setattr(access, "writer", lambda *a, **k: access.writer.__wrapped__(*a, connect_wait=0.1, **k))
        code = update_data.main()
    assert code == 2


def test_update_data_fails_when_openbb_is_unusable_and_never_takes_the_lock(app_env, monkeypatch):
    ensure_database(app_env)
    monkeypatch.setattr(update_data, "runtime_check", lambda: fake_openbb(False))
    monkeypatch.setattr(update_data.access, "writer", lambda *a, **k: pytest.fail("no lock without a working OpenBB"))
    assert update_data.main() == 1
