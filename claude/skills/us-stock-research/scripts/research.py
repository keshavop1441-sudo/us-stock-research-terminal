"""Launcher for the US Stock Research engine: ``python scripts/research.py <command> ...`` (see ``--help``).

Finds the engine in this order: a bundled copy next to this file (``_engine/``, present in the packaged Skill), then the
repository root (a source checkout). Prints one JSON envelope on stdout.

Standard library only, on purpose: if the third-party packages are not installed yet (a fresh Skill environment), the
engine import fails, and this launcher answers with ONE JSON error envelope (code ``DEPENDENCIES_MISSING``) that names
the exact hash-checked install command, instead of a raw traceback. It never installs anything itself.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET_PYTHON = "3.14"  # .python-version; the engine also imports on 3.11+ (see CLAUDE.md)

ENGINE_ROOT = None
for candidate in (HERE / "_engine", *HERE.parents):
    if (candidate / "app" / "cli" / "research.py").is_file():
        ENGINE_ROOT = candidate
        sys.path.insert(0, str(candidate))
        break
else:
    sys.stderr.write(
        '{"status": "ERROR", "errors": [{"code": "ENGINE_NOT_FOUND", "message": "engine package missing"}]}\n'
    )
    raise SystemExit(1)


def dependencies_missing_envelope(missing: str) -> dict:
    """The CLI's envelope shape (schema 1.0), built without importing the engine (which needs the missing packages)."""
    bundled = ENGINE_ROOT == HERE / "_engine"
    lock = "scripts/requirements.lock" if bundled else "requirements.lock"  # relative to the Skill folder / repo root
    fix = f"python -m pip install --require-hashes -r {lock}"
    version = ".".join(str(part) for part in sys.version_info[:3])
    message = (
        f"Python package {missing!r} is not installed. Run the fix command once (needs network access to PyPI), "
        "then repeat the original command. The lock file pins every package with hashes."
    )
    return {
        "schema_version": "1.0",
        "command": "launcher",
        "status": "ERROR",
        "generated_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0).isoformat(),
        "data": {
            "missing_module": missing,
            "python_version": version,
            "target_python": TARGET_PYTHON,
            "lock_file": lock,
            "fix_command": fix,
            "run_fix_from": "the Skill folder" if bundled else "the repository root",
        },
        "warnings": [],
        "errors": [{"code": "DEPENDENCIES_MISSING", "message": message}],
    }


try:
    from app.cli.research import main  # noqa: E402
except ModuleNotFoundError as exc:
    missing = (exc.name or "").split(".")[0]
    if not missing or missing == "app":  # our own code is broken: that is a bug, not a missing dependency
        raise
    sys.stdout.write(json.dumps(dependencies_missing_envelope(missing), ensure_ascii=False) + "\n")
    raise SystemExit(1) from exc

if __name__ == "__main__":
    raise SystemExit(main())
