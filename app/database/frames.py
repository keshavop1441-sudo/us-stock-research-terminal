"""DuckDB result -> Polars DataFrame, without pyarrow, pandas or numpy.

``cursor.pl()`` needs pyarrow, which this project does not declare. Instead rows are fetched as
plain Python tuples and typed from the column types DuckDB reports, so empty results still have
correct column types. This module must stay free of imports other than duckdb/polars.
"""

import duckdb
import polars as pl

_DTYPES: dict[str, pl.DataType] = {
    "BOOLEAN": pl.Boolean,
    "TINYINT": pl.Int64,
    "SMALLINT": pl.Int64,
    "INTEGER": pl.Int64,
    "BIGINT": pl.Int64,
    "FLOAT": pl.Float64,
    "DOUBLE": pl.Float64,
    "VARCHAR": pl.String,
    "DATE": pl.Date,
    "TIMESTAMP": pl.Datetime("us"),
}


def to_polars(cursor: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Materialise the cursor's pending result as a Polars DataFrame."""
    columns = [(name, str(type_code)) for name, type_code, *_ in cursor.description]
    rows = cursor.fetchall()
    schema = {name: _DTYPES.get(type_name) for name, type_name in columns}
    if any(dtype is None for dtype in schema.values()):  # unmapped type: let Polars infer that column
        frame = pl.DataFrame(rows, schema=[name for name, _ in columns], orient="row", infer_schema_length=None)
        return frame.cast({name: dtype for name, dtype in schema.items() if dtype is not None})
    return pl.DataFrame(rows, schema=schema, orient="row")
