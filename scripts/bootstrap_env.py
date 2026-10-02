"""Make the virtual environment usable, idempotently. Standard library only.

Run with the venv's Python by the .bat files. Safe to run repeatedly:

1. Refuses an interpreter that is not the required Python (exit 3; the .bat recreates the venv).
2. Installs ``requirements.lock`` with hash checking unless a marker proves the same lock was already
   installed successfully AND the required modules still import. A failed or interrupted install leaves no
   marker, so the next run installs again instead of assuming success.
3. Verifies the required modules import, then writes the marker.

Exit codes: 0 ready, 1 install/verification failed, 3 wrong Python version.
"""

import hashlib
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_PYTHON = (3, 14)  # keep in sync with .python-version (checked by tests)
LOCK_FILE = ROOT / "requirements.lock"
MARKER_FILE = ROOT / ".venv" / ".deps_installed"
# Imported for real (cheap). OpenBB packages are only located: importing OpenBB takes many seconds.
IMPORT_MODULES = ("streamlit", "duckdb", "polars", "plotly", "pydantic", "dotenv", "httpx", "filelock", "openbb_core")
LOCATE_MODULES = ("openbb_sec", "openbb_nasdaq", "openbb_cboe", "openbb_news")

Runner = Callable[..., subprocess.CompletedProcess]


def lock_hash(lock_file: Path) -> str:
    return hashlib.sha256(lock_file.read_bytes()).hexdigest()


def marker_matches(lock_file: Path, marker_file: Path) -> bool:
    try:
        return marker_file.read_text(encoding="utf-8").strip() == lock_hash(lock_file)
    except OSError:
        return False


def verify_imports(python: str, run: Runner = subprocess.run) -> tuple[bool, str]:
    code = (
        "import importlib, importlib.util, sys\n"
        f"for name in {IMPORT_MODULES!r}: importlib.import_module(name)\n"
        f"missing = [n for n in {LOCATE_MODULES!r} if importlib.util.find_spec(n) is None]\n"
        "sys.exit('missing: ' + ', '.join(missing) if missing else 0)\n"
    )
    result = run([python, "-c", code], capture_output=True, text=True)
    return result.returncode == 0, (result.stderr or result.stdout).strip()


def install(python: str, lock_file: Path, run: Runner = subprocess.run) -> bool:
    command = [python, "-m", "pip", "install", "--disable-pip-version-check", "--require-hashes", "-r", str(lock_file)]
    return run(command).returncode == 0


def ensure_environment(
    python: str = sys.executable,
    *,
    lock_file: Path = LOCK_FILE,
    marker_file: Path = MARKER_FILE,
    version: tuple[int, int] | None = None,
    run: Runner = subprocess.run,
) -> int:
    actual = version or sys.version_info[:2]
    if actual != REQUIRED_PYTHON:
        print(
            f"ERROR: Python {actual[0]}.{actual[1]} found, but {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]} is required."
        )
        return 3
    if not lock_file.is_file():
        print(f"ERROR: {lock_file.name} not found.")
        return 1
    if marker_matches(lock_file, marker_file) and verify_imports(python, run)[0]:
        print("Environment is ready.")
        return 0
    marker_file.unlink(missing_ok=True)  # until success is proven, nothing may claim the install worked
    print("Installing dependencies from requirements.lock ...")
    if not install(python, lock_file, run):
        print("ERROR: dependency installation failed. Fix the problem above and run this script again.")
        return 1
    ok, detail = verify_imports(python, run)
    if not ok:
        print(f"ERROR: dependencies installed but a required module cannot be imported: {detail}")
        return 1
    marker_file.write_text(lock_hash(lock_file), encoding="utf-8")
    print("Environment is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(ensure_environment())
