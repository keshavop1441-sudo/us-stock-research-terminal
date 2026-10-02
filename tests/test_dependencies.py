"""Reproducible dependencies: pinned direct requirements, hash-locked full resolution, nothing undeclared."""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# import name -> distribution name, where they differ
IMPORT_TO_DIST = {
    "dotenv": "python-dotenv",
    "openbb_core": "openbb-core",
    "openbb": "openbb-core",  # the `openbb` package ships inside openbb-core 2.x
    "openbb_sec": "openbb-sec",
    "openbb_nasdaq": "openbb-nasdaq",
    "openbb_cboe": "openbb-cboe",
    "openbb_news": "openbb-news",
    "yaml": "PyYAML",
}
# sibling helper modules in tests/ (imported as top-level names, e.g. ``import p0_fakes``) are local code too
LOCAL_PACKAGES = {"app", "scripts", "tests"} | {p.stem for p in (ROOT / "tests").glob("*.py")}


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def direct_requirements(path: Path) -> dict[str, str]:
    pins = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#")[0].strip()
        if not line or line.startswith("-r"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.!+\-]+)", line)
        assert match, f"{path.name}: direct requirement is not an exact pin: {raw!r}"
        pins[normalise(match[1])] = match[2]
    return pins


def lock_entries(path: Path) -> dict[str, dict]:
    """name -> {version, marker, hashes} for every pinned package in a uv lock file."""
    entries: dict[str, dict] = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        header = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;]+)(?:\s*;\s*(.*?))?\s*\\?$", line)
        if header:
            current = entries.setdefault(normalise(header[1]), {"version": header[2], "marker": header[3], "hashes": 0})
        elif current is not None and line.strip().startswith("--hash=sha256:"):
            current["hashes"] += 1
    return entries


def test_direct_requirements_are_exactly_pinned():
    assert direct_requirements(ROOT / "requirements.txt")
    assert direct_requirements(ROOT / "requirements-dev.txt")


def test_lock_files_pin_every_package_with_hashes_for_python_314():
    for name in ("requirements.lock", "requirements-dev.lock"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "--universal" in text.splitlines()[1] and "--python-version 3.14" in text.splitlines()[1]
        assert "--generate-hashes" in text.splitlines()[1]
        entries = lock_entries(ROOT / name)
        assert len(entries) > 50
        assert [n for n, e in entries.items() if e["hashes"] == 0] == [], f"{name}: packages without hashes"


def test_lock_files_contain_the_exact_direct_pins():
    runtime = direct_requirements(ROOT / "requirements.txt")
    dev = {**runtime, **direct_requirements(ROOT / "requirements-dev.txt")}
    for lock, direct in (("requirements.lock", runtime), ("requirements-dev.lock", dev)):
        entries = lock_entries(ROOT / lock)
        for name, version in direct.items():
            assert name in entries, f"{lock} is missing {name} (re-run the uv pip compile command)"
            assert entries[name]["version"] == version, f"{lock}: {name} {entries[name]['version']} != pinned {version}"


def test_lock_files_carry_platform_markers_so_windows_gets_its_own_packages():
    entries = lock_entries(ROOT / "requirements.lock")
    windows_only = {n for n, e in entries.items() if e["marker"] and "win32" in e["marker"]}
    assert windows_only, "no sys_platform == 'win32' packages: the lock is not cross-platform"
    assert all(entries[n]["hashes"] for n in windows_only)


def test_pyarrow_is_not_a_direct_dependency_and_nothing_imports_it():
    assert "pyarrow" not in direct_requirements(ROOT / "requirements.txt")


def third_party_imports(directory: Path) -> set[str]:
    found = set()
    for path in directory.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            found.update(n.split(".")[0] for n in names)
    return {n for n in found if n not in sys.stdlib_module_names and n not in LOCAL_PACKAGES}


def declared(name: str, requirements: dict[str, str]) -> bool:
    return normalise(IMPORT_TO_DIST.get(name, name)) in requirements


def test_every_third_party_import_in_the_application_is_a_declared_direct_dependency():
    runtime = direct_requirements(ROOT / "requirements.txt")
    undeclared = sorted(
        n for n in third_party_imports(ROOT / "app") | third_party_imports(ROOT / "scripts") if not declared(n, runtime)
    )
    assert not undeclared, f"imported but only available transitively (declare them in requirements.txt): {undeclared}"


def test_every_third_party_import_in_the_tests_is_declared():
    dev = {**direct_requirements(ROOT / "requirements.txt"), **direct_requirements(ROOT / "requirements-dev.txt")}
    undeclared = sorted(n for n in third_party_imports(ROOT / "tests") if not declared(n, dev))
    assert not undeclared, undeclared


def test_forbidden_infrastructure_is_not_a_direct_dependency():
    forbidden = {
        "fastapi",
        "flask",
        "django",
        "redis",
        "psycopg2",
        "psycopg",
        "pymongo",
        "docker",
        "celery",
        "chromadb",
        "faiss-cpu",
    }
    direct = set(direct_requirements(ROOT / "requirements.txt")) | set(
        direct_requirements(ROOT / "requirements-dev.txt")
    )
    assert not direct & forbidden


def test_ci_targets_python_314_on_linux_and_windows_with_hash_checked_installs():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'python-version: "3.14"' in workflow
    assert "3.13" not in workflow and "py313" not in workflow
    assert "windows-latest" in workflow and "ubuntu-latest" in workflow
    assert "--require-hashes -r requirements-dev.lock" in workflow
    assert "START_TERMINAL.bat /setup-only" in workflow and "UPDATE_DATA.bat /nopause" in workflow
    assert "git diff --exit-code requirements.lock requirements-dev.lock" in workflow  # lock freshness is enforced


def test_no_leftover_python_313_configuration():
    assert "py313" not in (ROOT / "ruff.toml").read_text(encoding="utf-8")
