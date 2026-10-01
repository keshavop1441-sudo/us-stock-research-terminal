"""Small helpers shared by the Streamlit pages."""

from datetime import datetime

import streamlit as st

from app.config import Settings

NA = "N/A"


def get_settings() -> Settings:
    return Settings.from_env()


def fmt_count(value: int | None) -> str:
    return NA if value is None else f"{value:,}"


def fmt_datetime(value: datetime | None, *, missing: str = NA) -> str:
    return missing if value is None else value.strftime("%Y-%m-%d %H:%M:%S")


def status_text(ok: bool | None, label: str) -> str:
    """Markdown with a colored dot: green = ok, red = problem, gray = unknown."""
    color = {True: "green", False: "red", None: "gray"}[ok]
    return f":{color}[●] {label}"


def not_implemented(feature: str, planned: list[str]) -> None:
    st.info(f"{feature} is not implemented yet. This page only defines the layout.")
    with st.expander("Planned"):
        st.markdown("\n".join(f"- {item}" for item in planned))
