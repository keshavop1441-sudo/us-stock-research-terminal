"""The repository's DuckDB -> Polars conversion must not depend on pyarrow/pandas/numpy."""

import ast
import datetime as dt
import subprocess
import sys
from pathlib import Path

import duckdb
import polars as pl

from app.database.frames import to_polars

ROOT = Path(__file__).resolve().parent.parent


def test_conversion_types_values_and_nulls():
    con = duckdb.connect()
    con.execute("CREATE TABLE t (i BIGINT, x DOUBLE, s VARCHAR, d DATE, ts TIMESTAMP, b BOOLEAN)")
    con.execute(
        "INSERT INTO t VALUES (NULL, NULL, NULL, NULL, NULL, NULL), "
        "(1, 1.5, 'a', '2024-01-02', '2024-01-02 03:04:05', true)"
    )
    frame = to_polars(con.execute("SELECT * FROM t ORDER BY i NULLS FIRST"))  # NULL row first would defeat inference
    assert frame.schema == {
        "i": pl.Int64, "x": pl.Float64, "s": pl.String, "d": pl.Date, "ts": pl.Datetime("us"), "b": pl.Boolean,
    }  # fmt: skip
    assert frame["d"].to_list() == [None, dt.date(2024, 1, 2)]
    assert frame["ts"].to_list()[1] == dt.datetime(2024, 1, 2, 3, 4, 5)


def test_empty_result_keeps_column_types():
    con = duckdb.connect()
    con.execute("CREATE TABLE t (i BIGINT, d DATE)")
    frame = to_polars(con.execute("SELECT * FROM t"))
    assert frame.is_empty()
    assert frame.schema == {"i": pl.Int64, "d": pl.Date}


def test_unmapped_type_falls_back_to_inference():
    frame = to_polars(duckdb.connect().execute("SELECT 5::HUGEINT AS h, 'x' AS s"))
    assert frame["h"].to_list() == [5]
    assert frame["s"].dtype == pl.String


def test_works_without_pyarrow_pandas_numpy():
    """Runs the conversion in a subprocess where those packages cannot be imported."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "minimal_frames_check.py")], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "minimal conversion OK" in result.stdout


def _app_modules():
    for path in (ROOT / "app").rglob("*.py"):
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_application_code_never_calls_arrow_backed_conversions():
    """``.pl()`` / ``.df()`` / ``.arrow()`` / ``.fetchdf()`` / ``.fetchnumpy()`` need undeclared packages."""
    banned = {"pl", "df", "arrow", "fetchdf", "fetch_arrow_table", "fetchnumpy", "to_pandas", "to_arrow"}
    offenders = [
        f"{path.relative_to(ROOT)}:{node.lineno}"
        for path, tree in _app_modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in banned
    ]
    assert not offenders, offenders


def test_application_code_does_not_import_pyarrow_pandas_or_numpy():
    banned = {"pyarrow", "pandas", "numpy"}
    offenders = []
    for path, tree in _app_modules():
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name.split(".")[0] in banned for name in names):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, offenders
