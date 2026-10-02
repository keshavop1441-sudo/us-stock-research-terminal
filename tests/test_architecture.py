"""Layering rules: cli -> services -> repositories -> database; research/ is pure. Enforced by reading the source."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
SKILL = ROOT / "claude" / "skills" / "us-stock-research"
# A string that STARTS like a SQL statement (prose that merely contains the word "drop" does not match).
SQL_STATEMENT = re.compile(
    r"^\s*(SELECT\s+(\*|[\w.]+\s*(,|\(|\bFROM\b))|INSERT\s+INTO\b|UPDATE\s+\w+\s+SET\b|DELETE\s+FROM\b"
    r"|CREATE\s+(TABLE|SEQUENCE|INDEX)\b|DROP\s+(TABLE|SEQUENCE|INDEX)\b|ALTER\s+TABLE\b|PRAGMA\s+\w+)",
    re.IGNORECASE | re.DOTALL,
)


def imports_of(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def string_constants(path: Path) -> list[str]:
    return [
        n.value
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def starts_with(imports: set[str], *prefixes: str) -> set[str]:
    return {i for i in imports if any(i == p or i.startswith(p + ".") for p in prefixes)}


def test_the_research_package_is_pure():
    """app/research holds the screen/evidence contracts: no I/O, no database, no providers, no environment."""
    forbidden = ("duckdb", "app.database", "app.data", "app.services", "app.cli", "openbb", "httpx",
                 "filelock", "dotenv", "app.config")  # fmt: skip
    for path in (APP / "research").glob("*.py"):
        assert not starts_with(imports_of(path), *forbidden), (path.name, starts_with(imports_of(path), *forbidden))
        ingestion = starts_with(imports_of(path), "app.ingestion") - {
            "app.ingestion.manifest",
            "app.ingestion.manifest.MANIFEST",
            "app.ingestion.manifest.P0Security",
        }
        assert not ingestion, (path.name, ingestion)  # only the pinned manifest (pure data) may be imported
        text = path.read_text(encoding="utf-8")
        assert "os.environ" not in text and "getenv" not in text, path.name


def test_the_cli_only_talks_to_services_and_never_opens_the_database():
    for path in (APP / "cli").glob("*.py"):
        imported = imports_of(path)
        assert not starts_with(imported, "duckdb", "app.database.access", "app.database.write_repository"), path.name
        assert not starts_with(imported, "app.database.read_repository", "app.database.connection"), path.name


def test_services_do_not_depend_on_the_cli():
    for path in (APP / "services").glob("*.py"):
        assert not starts_with(imports_of(path), "app.cli"), path.name


def test_only_the_service_layer_and_scripts_open_repositories():
    allowed = {APP / "database" / n for n in ("access.py", "__init__.py")} | set((APP / "services").glob("*.py"))
    for path in APP.rglob("*.py"):
        if path in allowed or path.parent == APP / "database":
            continue
        assert not starts_with(imports_of(path), "app.database.access", "app.database.write_repository"), path


def test_the_read_side_used_by_research_commands_cannot_write():
    """Research-side services read through ReadRepository; only the ingestion service and the P0 sequence write."""
    writers = {"ingestion_service.py", "db.py"}
    for path in (APP / "services").glob("research_*.py"):
        if path.name == "research_ingest_service.py":
            continue
        assert path.name not in writers
        imported = imports_of(path)
        assert not starts_with(imported, "app.database.write_repository", "app.services.db"), path.name
        assert "writer(" not in path.read_text(encoding="utf-8"), path.name


def test_the_skill_scripts_import_nothing_but_the_cli_entry_point():
    for path in SKILL.rglob("*.py"):
        imported = {i for i in imports_of(path) if i.split(".")[0] in {"app", "duckdb", "openbb", "httpx"}}
        assert imported <= {"app.cli.research", "app.cli.research.main"}, (path.name, imported)


def test_sql_lives_only_in_the_database_layer():
    for path in APP.rglob("*.py"):
        if path.parent == APP / "database":
            continue
        offenders = [t[:60] for t in string_constants(path) if SQL_STATEMENT.match(t)]
        assert not offenders, (path, offenders)


def test_no_generic_sql_shell_or_code_execution_surface_in_the_cli():
    """The command set is fixed: nothing in the CLI executes caller-supplied SQL, shell commands or Python."""
    text = (APP / "cli" / "research.py").read_text(encoding="utf-8")
    for needle in ("subprocess", "os.system", "eval(", "exec(", "execute(", "shell=True", "__import__"):
        assert needle not in text, needle


def test_the_sql_detector_itself_recognises_statements_but_not_prose():
    assert SQL_STATEMENT.match("SELECT a FROM t")
    assert SQL_STATEMENT.match("  insert into t values (1)")
    assert SQL_STATEMENT.match("DROP TABLE securities")
    assert not SQL_STATEMENT.match("Drop the cached reading (used by tests).")
    assert not SQL_STATEMENT.match("Select a security from the list")
