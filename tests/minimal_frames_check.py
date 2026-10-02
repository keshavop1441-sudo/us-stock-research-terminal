"""Standalone check (not collected by pytest): DuckDB -> Polars conversion in a MINIMAL environment.

Needs only duckdb and polars. pyarrow, pandas and numpy are made un-importable first, so this fails if the
conversion path ever starts depending on them. Run directly: ``python tests/minimal_frames_check.py``.
CI runs it in a fresh virtualenv that contains nothing but duckdb and polars; a pytest test runs it in a
subprocess as well.
"""

import datetime as dt
import sys
from pathlib import Path

for blocked in ("pyarrow", "pandas", "numpy"):
    sys.modules[blocked] = None  # any `import <blocked>` now raises ImportError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb  # noqa: E402
import polars as pl  # noqa: E402

from app.database.frames import to_polars  # noqa: E402


def main() -> None:
    con = duckdb.connect()
    con.execute("CREATE TABLE t (i BIGINT, n INTEGER, x DOUBLE, s VARCHAR, d DATE, ts TIMESTAMP, b BOOLEAN)")
    empty = to_polars(con.execute("SELECT * FROM t"))
    assert empty.is_empty()
    assert empty.schema == {
        "i": pl.Int64, "n": pl.Int64, "x": pl.Float64, "s": pl.String,
        "d": pl.Date, "ts": pl.Datetime("us"), "b": pl.Boolean,
    }, empty.schema  # fmt: skip

    con.execute("INSERT INTO t VALUES (1, 2, 1.5, 'a', '2024-01-02', '2024-01-02 03:04:05', true)")
    con.execute(
        "INSERT INTO t VALUES (2, NULL, NULL, NULL, NULL, NULL, NULL)"
    )  # NULL first column would mislead inference
    frame = to_polars(con.execute("SELECT * FROM t ORDER BY i DESC"))
    assert frame.schema == empty.schema, frame.schema
    assert frame["i"].to_list() == [2, 1]
    assert frame["d"].to_list() == [None, dt.date(2024, 1, 2)]
    assert frame["s"].null_count() == 1

    odd = to_polars(
        con.execute("SELECT 1::HUGEINT AS h, [1, 2] AS l")
    )  # a type without a mapping falls back to inference
    assert odd.columns == ["h", "l"] and odd.height == 1
    for blocked in ("pyarrow", "pandas", "numpy"):
        assert sys.modules[blocked] is None, f"{blocked} was imported"
    print("minimal conversion OK")


if __name__ == "__main__":
    main()
