"""Layering rules: ui -> services -> repositories -> database. Enforced by reading the source."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
# A string that STARTS like a SQL statement (prose that merely contains the word "drop" does not match).
SQL_STATEMENT = re.compile(
    r"^\s*(SELECT\s+(\*|[\w.]+\s*(,|\(|\bFROM\b))|INSERT\s+INTO\b|UPDATE\s+\w+\s+SET\b|DELETE\s+FROM\b"
    r"|CREATE\s+(TABLE|SEQUENCE|INDEX)\b|DROP\s+(TABLE|SEQUENCE|INDEX)\b|ALTER\s+TABLE\b|PRAGMA\s+\w+)",
    re.IGNORECASE | re.DOTALL,
)


def imports_of(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


def string_constants(path: Path) -> list[str]:
    return [
        n.value
        for n in ast.walk(ast.parse(path.read_text()))
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def starts_with(imports: set[str], *prefixes: str) -> set[str]:
    return {i for i in imports if any(i == p or i.startswith(p + ".") for p in prefixes)}


UI_AND_MAIN = [*(APP / "ui").glob("*.py"), APP / "main.py"]


def test_ui_pages_only_talk_to_services():
    forbidden = (
        "duckdb",
        "app.database",
        "app.data",
        "app.agent",
        "app.tools",
        "openbb",
        "openbb_core",
        "filelock",
        "httpx",
        "dotenv",
    )
    for path in UI_AND_MAIN:
        assert not starts_with(imports_of(path), *forbidden), (path.name, starts_with(imports_of(path), *forbidden))


def test_ui_pages_contain_no_sql():
    for path in UI_AND_MAIN:
        offenders = [t[:60] for t in string_constants(path) if SQL_STATEMENT.match(t)]
        assert not offenders, (path.name, offenders)


def test_ui_does_not_read_settings_or_environment_directly():
    for path in UI_AND_MAIN:
        assert not starts_with(imports_of(path), "app.config"), path.name
        assert "os.environ" not in path.read_text() and "getenv" not in path.read_text(), path.name


def test_services_do_not_depend_on_the_ui_or_streamlit():
    for path in (APP / "services").glob("*.py"):
        assert not starts_with(imports_of(path), "app.ui", "streamlit"), path.name


def test_only_the_service_layer_and_scripts_open_repositories():
    allowed = {APP / "database" / n for n in ("access.py", "__init__.py")} | set((APP / "services").glob("*.py"))
    for path in APP.rglob("*.py"):
        if path in allowed or path.parent == APP / "database":
            continue
        assert not starts_with(imports_of(path), "app.database.access", "app.database.write_repository"), path


def test_the_agent_and_tool_layers_cannot_reach_the_write_side_or_a_connection():
    for package in ("agent", "tools"):
        for path in (APP / package).glob("*.py"):
            imported = imports_of(path)
            assert not starts_with(
                imported, "duckdb", "app.database.write_repository", "app.database.access", "app.database.connection"
            ), (path.name, imported)
            assert not starts_with(imported, "app.services.db"), path.name  # write_access lives there


def test_sql_lives_only_in_the_database_layer():
    for path in APP.rglob("*.py"):
        if path.parent == APP / "database":
            continue
        offenders = [t[:60] for t in string_constants(path) if SQL_STATEMENT.match(t)]
        assert not offenders, (path, offenders)


def test_the_sql_detector_itself_recognises_statements_but_not_prose():
    assert SQL_STATEMENT.match("SELECT a FROM t")
    assert SQL_STATEMENT.match("  insert into t values (1)")
    assert SQL_STATEMENT.match("DROP TABLE securities")
    assert not SQL_STATEMENT.match("Drop the cached reading (used by tests).")
    assert not SQL_STATEMENT.match("Select a security from the list")
