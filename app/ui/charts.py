"""Plotly figures."""

import plotly.graph_objects as go
import polars as pl


def price_chart(prices: pl.DataFrame, title: str) -> go.Figure:
    """Close-price line from a ``price_history`` frame. NULL closes are left as gaps."""
    figure = go.Figure(
        go.Scatter(
            x=prices["trade_date"].to_list(),
            y=prices["close"].to_list(),
            mode="lines",
            name="Close",
            connectgaps=False,
        )
    )
    figure.update_layout(title=title, xaxis_title="Date", yaxis_title="Close (USD)", height=420)
    return figure
