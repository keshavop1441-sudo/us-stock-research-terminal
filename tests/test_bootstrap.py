"""scripts/bootstrap_env.py: idempotent, recovers from failed installs. (stdlib-only; Windows-independent logic.)"""

import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"script_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load_script("bootstrap_env")


class FakeRunner:
    """Stands in for subprocess.run. ``pip_ok`` / ``imports_ok`` can be changed between calls."""

    def __init__(self, pip_ok=True, imports_ok=True):
        self.pip_ok, self.imports_ok, self.calls = pip_ok, imports_ok, []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        ok = self.pip_ok if "pip" in command else self.imports_ok
        return subprocess.CompletedProcess(command, 0 if ok else 1, stdout="", stderr="" if ok else "boom")

    @property
    def pip_calls(self):
        return [c for c in self.calls if "pip" in c]


@pytest.fixture
def files(tmp_path):
    lock = tmp_path / "requirements.lock"
    lock.write_text("example==1.0 \\\n    --hash=sha256:abc\n")
    return lock, tmp_path / ".venv" / ".deps_installed"


def run(files, runner, **kwargs):
    lock, marker = files
    marker.parent.mkdir(exist_ok=True)
    return bootstrap.ensure_environment(
        "python", lock_file=lock, marker_file=marker, version=(3, 14), run=runner, **kwargs
    )


def test_fresh_install_uses_hashes_and_writes_the_marker(files):
    runner = FakeRunner()
    assert run(files, runner) == 0
    (pip,) = runner.pip_calls
    assert "--require-hashes" in pip and str(files[0]) in pip and pip[pip.index("-r") + 1] == str(files[0])
    assert files[1].read_text(encoding="utf-8") == hashlib.sha256(files[0].read_bytes()).hexdigest()


def test_second_run_is_a_no_op(files):
    run(files, FakeRunner())
    runner = FakeRunner()
    assert run(files, runner) == 0
    assert runner.pip_calls == []  # nothing reinstalled


def test_a_broken_environment_is_reinstalled_even_though_the_marker_matches(files):
    run(files, FakeRunner())
    runner = FakeRunner(imports_ok=False)
    assert run(files, runner) == 1  # imports still fail after the reinstall: reported, not hidden
    assert len(runner.pip_calls) == 1  # but it did try to repair
    assert not files[1].exists()


def test_a_changed_lock_file_triggers_a_reinstall(files):
    run(files, FakeRunner())
    files[0].write_text("example==2.0 \\\n    --hash=sha256:def\n")
    runner = FakeRunner()
    assert run(files, runner) == 0
    assert len(runner.pip_calls) == 1


def test_failed_install_fails_loudly_leaves_no_marker_and_the_next_run_recovers(files):
    runner = FakeRunner(pip_ok=False)
    assert run(files, runner) == 1
    assert not files[1].exists()  # nothing may claim the install worked
    recovered = FakeRunner()  # e.g. the network is back
    assert run(files, recovered) == 0
    assert len(recovered.pip_calls) == 1 and files[1].exists()


def test_failed_reinstall_removes_a_previous_marker(files):
    run(files, FakeRunner())
    files[0].write_text("example==2.0 \\\n    --hash=sha256:def\n")
    assert run(files, FakeRunner(pip_ok=False)) == 1
    assert not files[1].exists()


def test_installed_but_not_importable_is_a_failure_without_marker(files):
    assert run(files, FakeRunner(imports_ok=False)) == 1
    assert not files[1].exists()


def test_wrong_python_version_is_refused_without_touching_anything(files):
    lock, marker = files
    runner = FakeRunner()
    code = bootstrap.ensure_environment("python", lock_file=lock, marker_file=marker, version=(3, 13), run=runner)
    assert code == 3 and runner.calls == [] and not marker.exists()


def test_missing_lock_file_fails(tmp_path):
    runner = FakeRunner()
    code = bootstrap.ensure_environment(
        "python", lock_file=tmp_path / "nope.lock", marker_file=tmp_path / "m", version=(3, 14), run=runner
    )
    assert code == 1 and runner.calls == []


def test_the_real_import_check_runs_and_passes_in_this_environment():
    ok, detail = bootstrap.verify_imports(sys.executable)
    assert ok, detail


def test_the_real_import_check_detects_a_missing_module(monkeypatch):
    monkeypatch.setattr(bootstrap, "IMPORT_MODULES", ("definitely_not_installed_module_xyz",))
    ok, detail = bootstrap.verify_imports(sys.executable)
    assert not ok and "definitely_not_installed_module_xyz" in detail


def test_required_python_matches_the_declared_target():
    major, minor = bootstrap.REQUIRED_PYTHON
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == f"{major}.{minor}"
    assert f'target-version = "py{major}{minor}"' in (ROOT / "ruff.toml").read_text(encoding="utf-8")


def test_marker_and_lock_paths_point_at_the_project():
    assert bootstrap.LOCK_FILE == ROOT / "requirements.lock"
    assert bootstrap.MARKER_FILE.parent.name == ".venv"
