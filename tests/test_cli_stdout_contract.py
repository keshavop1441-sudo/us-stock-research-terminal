"""The CLI contract: stdout carries ONE JSON envelope and nothing else; diagnostics go to stderr.

Regression: ``import openbb`` calls ``print()`` when the installed extensions differ from the built static assets (the
first run after an install), e.g. ``Extensions to add: cboe@2.0.0, ...`` and ``Building...``. That text used to land on
stdout in front of the JSON, so a machine reader could not parse the result.

OpenBB is replaced by a tiny stand-in module that prints exactly those lines when it is imported, so the tests are
hermetic (no network, no real OpenBB build) yet exercise the real import path of the real CLI."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.data import openbb_client

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "claude" / "skills" / "us-stock-research" / "scripts" / "research.py"

NOISE = 'print("Extensions to add: cboe@2.0.0, nasdaq@2.0.0, news@2.0.0, sec@2.0.0")\nprint("\\nBuilding...")\n'

# What ``runtime_check`` reads from ``obb.coverage`` (providers, and the router name in 'obb.<router>.<command>').
HEALTHY_OPENBB = (
    NOISE
    + """
class _Coverage:
    providers = ["cboe", "nasdaq", "sec"]
    commands = ["obb.cboe.a", "obb.nasdaq.a", "obb.news.a", "obb.sec.a"]


class _Obb:
    coverage = _Coverage()


obb = _Obb()
"""
)
BROKEN_OPENBB = NOISE + 'raise RuntimeError("openbb build failed (simulated)")\n'


def run_cli(tmp_path, openbb_source, *args):
    """Run the real launcher in a subprocess where ``import openbb`` resolves to the stand-in module."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "openbb.py").write_text(openbb_source)
    env = {k: v for k, v in os.environ.items() if k != "SEC_USER_AGENT"}
    env["PYTHONPATH"] = os.pathsep.join([str(shim), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])])
    return subprocess.run(
        [sys.executable, str(LAUNCHER), "--db", str(tmp_path / "x.duckdb"), *args],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
        env=env,
    )


@pytest.mark.parametrize("pretty", [False, True], ids=["compact", "pretty"])
def test_stdout_is_exactly_one_json_object_even_when_importing_openbb_prints(tmp_path, pretty):
    flags = ["--pretty"] if pretty else []
    done = run_cli(tmp_path, HEALTHY_OPENBB, *flags, "doctor", "--deep")

    assert done.returncode == 0, done.stderr
    # json.loads on the WHOLE of stdout: any text before or after the object makes this raise.
    envelope = json.loads(done.stdout)
    assert done.stdout.startswith("{") and done.stdout.endswith("}\n")
    assert envelope["command"] == "doctor" and envelope["schema_version"] == "1.0"
    checks = {c["name"]: c for c in envelope["data"]["checks"]}
    assert checks["openbb_runtime"]["status"] == "OK"  # the stand-in really was imported by the CLI

    assert "Extensions to add" not in done.stdout and "Building..." not in done.stdout
    # Not suppressed: the build messages are kept as a diagnostic on stderr.
    assert "Extensions to add: cboe@2.0.0" in done.stderr and "Building..." in done.stderr
    # --pretty keeps its meaning: indented over many lines; the compact form stays a single line.
    assert (done.stdout.count("\n") > 10) is pretty
    if not pretty:
        assert done.stdout.count("\n") == 1


def test_an_openbb_import_failure_is_still_reported_in_the_json_result(tmp_path):
    done = run_cli(tmp_path, BROKEN_OPENBB, "doctor", "--deep")

    envelope = json.loads(done.stdout)  # still one clean JSON object
    checks = {c["name"]: c for c in envelope["data"]["checks"]}
    assert checks["openbb_runtime"]["status"] == "FAIL"
    assert "openbb build failed (simulated)" in checks["openbb_runtime"]["detail"]  # the real error is not hidden
    assert "Extensions to add" not in done.stdout
    assert "Extensions to add" in done.stderr


@pytest.fixture
def fake_openbb(tmp_path, monkeypatch):
    """Make ``import openbb`` inside this process resolve to a stand-in module; restore everything afterwards."""

    def install(source):
        (tmp_path / "openbb.py").write_text(source)
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.delitem(sys.modules, "openbb", raising=False)  # restored by monkeypatch if it was imported
        openbb_client.get_obb.cache_clear()

    yield install
    sys.modules.pop("openbb", None)  # drop the stand-in
    openbb_client.get_obb.cache_clear()  # never leave the stand-in cached for other tests


def test_get_obb_sends_import_time_prints_to_stderr_and_returns_obb(fake_openbb, capsys):
    fake_openbb(NOISE + "obb = object()\n")

    obb = openbb_client.get_obb()

    assert obb is sys.modules["openbb"].obb
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Extensions to add: cboe@2.0.0, nasdaq@2.0.0, news@2.0.0, sec@2.0.0" in captured.err
    assert "Building..." in captured.err
    print("after")  # stdout is back to normal once the import is done
    assert capsys.readouterr().out == "after\n"


def test_get_obb_does_not_swallow_errors_and_restores_stdout(fake_openbb, capsys):
    fake_openbb(BROKEN_OPENBB)

    with pytest.raises(RuntimeError, match="openbb build failed"):
        openbb_client.get_obb()

    assert capsys.readouterr().out == ""
    print("after")
    assert capsys.readouterr().out == "after\n"
