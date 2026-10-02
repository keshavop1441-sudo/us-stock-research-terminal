import polars as pl
import streamlit as st

from app.ui.components import not_implemented

RESULT_COLUMNS = {
    "Ticker": pl.String,
    "Company": pl.String,
    "Sector": pl.String,
    "Off 52-week high %": pl.Float64,
    "Revenue growth %": pl.Float64,
    "EPS growth %": pl.Float64,
    "Free cash flow": pl.Float64,
    "P/S": pl.Float64,
}

st.title("Screener")
not_implemented(
    "The financial screening engine",
    [
        "Criteria parsed from natural language into a validated, structured screen",
        "Ratios and growth rates calculated at query time from raw stored facts",
        "Distance from 52-week high, margin trends, leverage changes",
        "Saved screens and results linked to research runs",
    ],
)
st.subheader("Results")
st.dataframe(pl.DataFrame(schema=RESULT_COLUMNS), hide_index=True, width="stretch")
st.caption("No results: nothing has been screened.")
