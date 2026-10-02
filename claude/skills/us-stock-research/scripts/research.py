"""Launcher for the US Stock Research engine: ``python scripts/research.py <command> ...`` (see ``--help``).

Finds the engine in this order: a bundled copy next to this file (``_engine/``, present in the packaged Skill), then the
repository root (a source checkout). Prints one JSON envelope on stdout.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (HERE / "_engine", *HERE.parents):
    if (candidate / "app" / "cli" / "research.py").is_file():
        sys.path.insert(0, str(candidate))
        break
else:
    sys.stderr.write(
        '{"status": "ERROR", "errors": [{"code": "ENGINE_NOT_FOUND", "message": "engine package missing"}]}\n'
    )
    raise SystemExit(1)

from app.cli.research import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
