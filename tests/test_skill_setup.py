"""Fresh-environment setup of the Skill: packages come from the hash-checked lock, a missing package is reported as ONE
JSON envelope (never a traceback), and frontmatter never carries an unsupported ``dependencies`` key."""

import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.test_skill_package import SKILL, pk

ROOT = Path(__file__).resolve().parent.parent
FIX = "python -m pip install --require-hashes -r scripts/requirements.lock"


def run_launcher(launcher: Path, cwd: Path, shim: Path, *args: str):
    """Run the real launcher where ``import dotenv`` (the engine's first third-party import) fails like a fresh env."""
    (shim / "dotenv.py").write_text("raise ModuleNotFoundError(\"No module named 'dotenv'\", name='dotenv')\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(shim)
    return subprocess.run(
        [sys.executable, str(launcher), *args], capture_output=True, text=True, cwd=cwd, env=env, timeout=120
    )


@pytest.fixture
def unpacked(tmp_path):
    built = tmp_path / "us-stock-research.zip"
    assert pk.main(["--out", str(built)]) == 0
    with zipfile.ZipFile(built) as archive:
        archive.extractall(tmp_path / "skills")
    (tmp_path / "shim").mkdir()
    (tmp_path / "work").mkdir()
    return tmp_path / "skills" / "us-stock-research" / "scripts" / "research.py", tmp_path


def test_a_missing_package_gives_one_json_envelope_with_the_exact_fix(unpacked):
    launcher, tmp = unpacked
    done = run_launcher(launcher, tmp / "work", tmp / "shim", "catalog")
    assert done.returncode == 1
    assert "Traceback" not in done.stdout + done.stderr
    envelope = json.loads(done.stdout)  # stdout is exactly ONE JSON object
    assert done.stdout.count("\n") == 1
    assert envelope["status"] == "ERROR" and envelope["schema_version"] == "1.0"
    assert envelope["errors"][0]["code"] == "DEPENDENCIES_MISSING"
    data = envelope["data"]
    assert data["missing_module"] == "dotenv" and data["fix_command"] == FIX
    assert data["lock_file"] == "scripts/requirements.lock" and data["target_python"] == "3.14"
    assert set(envelope) == {"schema_version", "command", "status", "generated_at", "data", "warnings", "errors"}


def test_the_reported_lock_file_exists_in_the_package_and_is_hash_pinned(unpacked):
    launcher, _ = unpacked
    lock = launcher.parent / "requirements.lock"
    assert lock.is_file() and "--require-hashes" in FIX
    text = lock.read_text(encoding="utf-8")
    assert "--hash=sha256:" in text and "pydantic==" in text and "python-dotenv==" in text
    assert text == (ROOT / "requirements.lock").read_text(encoding="utf-8")  # the repository's lock, unmodified


def test_the_error_envelope_leaks_no_local_paths_or_secrets(unpacked):
    launcher, tmp = unpacked
    out = run_launcher(launcher, tmp / "work", tmp / "shim", "catalog").stdout
    assert not pk.LOCAL_PATH.search(out) and not pk.SECRET.search(out) and str(tmp) not in out


def test_the_source_checkout_launcher_points_at_the_root_lock(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "shim").mkdir()
    done = run_launcher(SKILL / "scripts" / "research.py", tmp_path / "work", tmp_path / "shim", "catalog")
    data = json.loads(done.stdout)["data"]
    assert data["lock_file"] == "requirements.lock" and (ROOT / data["lock_file"]).is_file()
    assert data["run_fix_from"] == "the repository root"


def test_a_missing_package_of_our_own_is_not_reported_as_a_dependency(unpacked):
    launcher, tmp = unpacked
    engine = launcher.parent / "_engine" / "app" / "cli" / "research.py"
    engine.write_text("import app.not_a_module\n")
    done = run_launcher(launcher, tmp / "work", tmp / "shim", "catalog")
    assert done.returncode != 0 and "DEPENDENCIES_MISSING" not in done.stdout and "Traceback" in done.stderr


def test_skill_md_tells_claude_to_install_from_the_hashed_lock_only():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert FIX in text and "DEPENDENCIES_MISSING" in text
    assert "pip install -r scripts/requirements.txt" not in text  # unhashed, direct pins only
    assert not re.search(r"pip install(?! --require-hashes)(?!.*--require-hashes)", text.replace(FIX, ""))
    assert "3.14" in text and "3.11" in text  # Python policy and the supported floor
    assert text.index("doctor") < text.index(FIX)  # try first, install only when it is missing


def test_frontmatter_has_no_dependencies_key_and_the_packager_rejects_unknown_keys():
    head = (SKILL / "SKILL.md").read_text(encoding="utf-8").split("\n---\n")[0]
    assert "dependencies" not in head
    ok = "---\nname: us-stock-research\ndescription: x\n---\n"
    assert pk.validate_skill_md(ok)["name"] == "us-stock-research"
    with pytest.raises(pk.PackagingError, match="unsupported keys"):
        pk.validate_skill_md("---\nname: us-stock-research\ndescription: x\ndependencies: pydantic\n---\n")


def test_the_python_policy_is_unchanged_and_the_launcher_agrees():
    assert (ROOT / ".python-version").read_text().strip() == "3.14"
    assert 'TARGET_PYTHON = "3.14"' in (SKILL / "scripts" / "research.py").read_text(encoding="utf-8")
    assert "python-version 3.14" in (ROOT / "requirements.lock").read_text(encoding="utf-8")
