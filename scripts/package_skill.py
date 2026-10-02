"""Build the uploadable Claude Skill ZIP, reproducibly. Standard library only.

    python scripts/package_skill.py [--out dist/us-stock-research.zip] [--verify]

The archive has ONE top-level folder, ``us-stock-research/`` (it must equal the Skill's ``name``), containing:

    SKILL.md  references/  templates/  scripts/research.py  scripts/requirements.txt  scripts/requirements.lock
    scripts/_engine/app/**     the deterministic engine (the repository's ``app`` package), bundled so the Skill runs
                               without the repository

Reproducible: entries are sorted, timestamps and permissions are fixed, compression is fixed, so the same sources give
byte-identical output (``--verify`` rebuilds and compares, and checks the written ``.sha256``). The build FAILS if a
file looks like a secret or contains a local absolute path, if SKILL.md is invalid, or if a forbidden file would be
packaged (.git, caches, virtual environments, databases, tests, .env).
"""

import argparse
import hashlib
import io
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "claude" / "skills" / "us-stock-research"
SKILL_NAME = "us-stock-research"
DEFAULT_OUT = ROOT / "dist" / f"{SKILL_NAME}.zip"
ZIP_TIME = (2020, 1, 1, 0, 0, 0)
FORBIDDEN_PARTS = {".git", "__pycache__", ".venv", "venv", ".pytest_cache", ".ruff_cache", "tests", "data", "dist",
                   "research_data", "node_modules"}  # fmt: skip
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".duckdb", ".wal", ".log", ".env", ".key", ".pem"}
FORBIDDEN_NAMES = {".env", ".DS_Store", "Thumbs.db"}
LOCAL_PATH = re.compile(r"(/home/[A-Za-z0-9_.-]+|/Users/[A-Za-z0-9_.-]+|[A-Za-z]:\\Users\\|/root/|/tmp/claude)")
SECRET = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{10,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
EXECUTABLE = {"scripts/research.py"}
# The frontmatter keys the Agent Skills format defines. There is NO ``dependencies`` key: Claude does not install
# packages from frontmatter, and validators reject unknown keys. Dependencies are installed from the hash-locked
# ``scripts/requirements.lock`` by the instructions in the SKILL.md body.
ALLOWED_FRONTMATTER = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


class PackagingError(Exception):
    pass


def validate_skill_md(text: str) -> dict[str, str]:
    """Frontmatter check against the documented constraints (name, description). Returns the two fields."""
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise PackagingError("SKILL.md has no YAML frontmatter")
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith(" "):
            fields[key.strip()] = value.strip()
    unknown = sorted(set(fields) - ALLOWED_FRONTMATTER)
    if unknown:
        raise PackagingError(
            f"SKILL.md frontmatter has unsupported keys {unknown}; allowed: {sorted(ALLOWED_FRONTMATTER)}"
        )
    name, description = fields.get("name", ""), fields.get("description", "")
    if name != SKILL_NAME:
        raise PackagingError(f"SKILL.md name must be {SKILL_NAME!r} (the folder name), got {name!r}")
    if not re.fullmatch(r"[a-z0-9-]{1,64}", name) or "claude" in name or "anthropic" in name:
        raise PackagingError(f"invalid skill name {name!r}")
    if not description or len(description) > 1024 or re.search(r"<[^>]+>", description):
        raise PackagingError("description must be non-empty, <= 1024 characters and contain no XML tags")
    return {"name": name, "description": description}


def collect() -> dict[str, Path]:
    """archive path (relative to the skill folder) -> source file."""
    files: dict[str, Path] = {}
    for name in ("SKILL.md",):
        files[name] = SKILL_DIR / name
    for sub in ("references", "templates"):
        for path in sorted((SKILL_DIR / sub).rglob("*")):
            if path.is_file():
                files[path.relative_to(SKILL_DIR).as_posix()] = path
    files["scripts/research.py"] = SKILL_DIR / "scripts" / "research.py"
    files["scripts/requirements.txt"] = ROOT / "requirements.txt"
    files["scripts/requirements.lock"] = ROOT / "requirements.lock"
    for path in sorted((ROOT / "app").rglob("*.py")):
        files[f"scripts/_engine/{path.relative_to(ROOT).as_posix()}"] = path
    return dict(sorted(files.items()))


def check_file(arcname: str, path: Path) -> bytes:
    # ``app/data`` is a real engine package (provider clients), not the local data directory
    engine_prefix = "scripts/_engine/app/"
    in_engine = arcname.startswith(engine_prefix)
    parts = set(arcname.removeprefix(engine_prefix).split("/")[1:] if in_engine else arcname.split("/"))
    name = path.name
    if parts & FORBIDDEN_PARTS or path.suffix in FORBIDDEN_SUFFIXES or name in FORBIDDEN_NAMES:
        raise PackagingError(f"forbidden file in the package: {arcname}")
    if not path.is_file():
        raise PackagingError(f"missing file: {arcname}")
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackagingError(f"{arcname} is not UTF-8 text (binary files are not packaged)") from exc
    if "\r" in text:
        data = text.replace("\r\n", "\n").encode("utf-8")  # reproducible across Windows/Unix checkouts
        text = data.decode("utf-8")
    if LOCAL_PATH.search(text):
        raise PackagingError(f"{arcname} contains a local absolute path")
    if SECRET.search(text):
        raise PackagingError(f"{arcname} looks like it contains a secret")
    return data


def build_bytes() -> tuple[bytes, list[str]]:
    files = collect()
    validate_skill_md(files["SKILL.md"].read_text(encoding="utf-8"))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for arcname, path in files.items():
            data = check_file(arcname, path)
            info = zipfile.ZipInfo(f"{SKILL_NAME}/{arcname}", date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # fixed: Unix, whatever platform builds it
            info.external_attr = (0o755 if arcname in EXECUTABLE else 0o644) << 16
            archive.writestr(info, data, compresslevel=9)
    return buffer.getvalue(), list(files)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--verify", action="store_true", help="rebuild and require byte-identical output")
    args = parser.parse_args(argv)
    try:
        data, names = build_bytes()
        if args.verify:
            again, _ = build_bytes()
            if again != data:
                raise PackagingError("two builds differ: the package is not reproducible")
            if args.out.is_file() and args.out.read_bytes() != data:
                raise PackagingError(f"{args.out.name} is stale: rebuild it")
    except PackagingError as exc:
        print(f"PACKAGING FAILED: {exc}", file=sys.stderr)
        return 1
    digest = sha256_of(data)
    if not args.verify:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(data)
        args.out.with_name(args.out.name + ".sha256").write_text(f"{digest}  {args.out.name}\n", encoding="utf-8")
    print(f"{args.out.name}: {len(names)} files, {len(data)} bytes, sha256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
