import streamlit as st

from app.services import watchlist_service
from app.services.errors import DataUnavailableError

st.title("Watchlists")

with st.form("new_watchlist", clear_on_submit=True):
    name = st.text_input("New watchlist name", max_chars=80)
    description = st.text_input("Description (optional)", max_chars=200)
    create = st.form_submit_button("Create watchlist", type="primary")

if create:
    try:
        watchlist_service.create_watchlist(name, description)
        st.success(f"Created watchlist '{name.strip()}'.")
    except ValueError as exc:
        st.warning(str(exc))
    except DataUnavailableError as exc:
        st.warning(f"The watchlist could not be saved: {exc}")

try:
    watchlists = watchlist_service.list_watchlists()
except DataUnavailableError as exc:
    st.warning(f"Watchlists are unavailable: {exc}")
    st.stop()
except Exception as exc:  # noqa: BLE001
    st.error(f"Database error: {type(exc).__name__}: {exc}")
    st.stop()

if watchlists.is_empty():
    st.caption("No watchlists yet.")
else:
    st.dataframe(watchlists, hide_index=True, width="stretch")
st.info("Adding securities to a watchlist is not implemented yet; it needs synchronized securities data.")
