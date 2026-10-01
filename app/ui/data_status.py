import streamlit as st

from app.data.openbb_client import runtime_check
from app.services.status_service import collect_data_status
from app.ui.components import NA, fmt_count, fmt_datetime, get_settings, status_text

RUNTIME_KEY = "openbb_runtime_status"

settings = get_settings()
status = collect_data_status(settings)

st.title("Data Status")
st.button("Refresh", icon=":material/refresh:")

st.subheader("Database")
db = status.database
if db.error:
    db_label = f"Error - {db.error}"
elif not db.exists:
    db_label = "Not created yet"
elif not db.initialized:
    db_label = "File exists but schema is not initialised"
else:
    db_label = f"Ready (schema v{db.schema_version})"
st.markdown(status_text(db.initialized and not db.error, db_label))
st.text_input("Database path", value=str(db.path), disabled=True)

counts = status.counts
columns = st.columns(5)
columns[0].metric("Securities", fmt_count(counts.get("securities")))
columns[1].metric("Price records", fmt_count(counts.get("price_daily")))
columns[2].metric("Financial facts", fmt_count(counts.get("financial_facts")))
columns[3].metric("Filings", fmt_count(counts.get("filings")))
columns[4].metric("Last synchronization", fmt_datetime(status.last_sync, missing="Never"))
if counts:
    with st.expander("All tables"):
        st.dataframe(
            [{"table": name, "rows": fmt_count(rows)} for name, rows in counts.items()],
            hide_index=True,
            width="stretch",
        )

st.subheader("OpenBB")
openbb = st.session_state.get(RUNTIME_KEY) or status.openbb
if openbb.runtime_checked:
    openbb_ok, openbb_label = openbb.runtime_ok, openbb.detail
elif openbb.all_installed:
    openbb_ok, openbb_label = True, "All required packages installed (runtime not checked)"
else:
    openbb_ok, openbb_label = False, "Required packages missing"
st.markdown(status_text(openbb_ok, openbb_label))
st.dataframe(
    [{"package": p.name, "version": p.version or "Not installed"} for p in openbb.packages],
    hide_index=True,
    width="stretch",
)
if st.button(
    "Run OpenBB runtime check", help="Imports OpenBB V5 and lists the loaded providers. Takes several seconds."
):
    with st.spinner("Importing OpenBB..."):
        st.session_state[RUNTIME_KEY] = runtime_check()
    st.rerun()
if openbb.runtime_checked:
    st.write(f"Providers: {', '.join(openbb.providers) or NA}")
    st.write(f"Routers: {', '.join(openbb.routers) or NA}")

st.subheader("Ollama")
ollama = status.ollama
st.markdown(status_text(ollama.available, ollama.detail))
st.caption(f"{ollama.base_url} - model: {ollama.configured_model}")
