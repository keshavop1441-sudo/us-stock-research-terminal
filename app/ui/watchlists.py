import streamlit as st

from app.database import repository
from app.database.connection import connect
from app.ui.components import get_settings

db_path = get_settings().resolved_database_path

st.title("Watchlists")

with st.form("new_watchlist", clear_on_submit=True):
    name = st.text_input("New watchlist name", max_chars=80)
    description = st.text_input("Description (optional)", max_chars=200)
    create = st.form_submit_button("Create watchlist", type="primary")

try:
    with connect(db_path) as con:
        if create:
            try:
                repository.create_watchlist(con, name, description)
                st.success(f"Created watchlist '{name.strip()}'.")
            except ValueError as exc:
                st.warning(str(exc))
        watchlists = repository.list_watchlists(con)
except Exception as exc:  # noqa: BLE001
    st.error(f"Database error: {type(exc).__name__}: {exc}")
    st.stop()

if watchlists.is_empty():
    st.caption("No watchlists yet.")
else:
    st.dataframe(watchlists, hide_index=True, width="stretch")
st.info("Adding securities to a watchlist is not implemented yet; it needs synchronized securities data.")
