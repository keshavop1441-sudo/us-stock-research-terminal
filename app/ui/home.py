import streamlit as st

from app.database import repository
from app.database.connection import connect
from app.ui.components import get_settings

EXAMPLE_PROMPTS = [
    "US technology companies with revenue growth above 15%.",
    "Profitable software companies with P/S below 5.",
    "Technology stocks down more than 30% from their 52-week high.",
]
QUERY_KEY = "home_query"
db_path = get_settings().resolved_database_path


def use_example(text: str) -> None:
    st.session_state[QUERY_KEY] = text


st.title("US STOCK RESEARCH TERMINAL")
st.caption("Describe the stocks you want to research in plain English.")

with st.form("home_search"):
    st.text_area(
        "What are you looking for?",
        key=QUERY_KEY,
        height=140,
        max_chars=repository.QUERY_TEXT_MAX_LENGTH,
        placeholder="US technology companies with revenue growth above 15%...",
    )
    submitted = st.form_submit_button("Search", type="primary")

st.markdown("**Try an example**")
for index, prompt in enumerate(EXAMPLE_PROMPTS):
    st.button(prompt, key=f"example_{index}", on_click=use_example, args=(prompt,))

if submitted:
    text = st.session_state.get(QUERY_KEY, "").strip()
    if not text:
        st.warning("Type a request first.")
    else:
        try:
            with connect(db_path) as con:
                query_id = repository.record_query(con, text)
        except Exception as exc:  # noqa: BLE001 - show the problem, keep the page usable
            st.error(f"Could not save the request: {type(exc).__name__}: {exc}")
        else:
            st.info(
                f"Request saved as query #{query_id}. Natural-language interpretation and "
                "screening are not implemented yet, so no results are available."
            )

st.subheader("Recent requests")
try:
    with connect(db_path) as con:
        recent = repository.recent_queries(con, limit=10)
except Exception as exc:  # noqa: BLE001
    st.warning(f"Query history is unavailable: {type(exc).__name__}: {exc}")
else:
    if recent.is_empty():
        st.caption("No requests yet.")
    else:
        st.dataframe(recent, hide_index=True, width="stretch")
