import streamlit as st

from app.services.settings_service import get_settings
from app.services.status_service import collect_data_status, run_openbb_runtime_check
from app.ui.components import NA, fmt_count, fmt_datetime, status_text

RUNTIME_KEY = "openbb_runtime_status"

settings = get_settings()
status = collect_data_status(settings)

st.title("Data Status")
st.button("Refresh", icon=":material/refresh:")

st.subheader("Database")
db = status.database
if status.data_state == "stale" or (status.data_state == "unavailable" and db.exists):
    st.warning(status.notice)
elif status.refresh.active:
    st.info(
        f"A write operation is in progress ({status.refresh.operation or 'unknown'}). Figures below were just read."
    )

if db.error:
    db_label = f"Error - {db.error}"
elif not db.exists:
    db_label = "Not created yet"
elif status.data_state == "stale":
    db_label = "Busy - showing cached figures"
elif not db.initialized:
    db_label = "Unavailable" if status.data_state == "unavailable" else "File exists but schema is not initialised"
else:
    db_label = f"Ready (schema v{db.schema_version})"
st.markdown(status_text(None if status.data_state != "live" else db.initialized and not db.error, db_label))
st.text_input("Database path", value=str(db.path), disabled=True)

counts = status.counts
suffix = " (cached)" if status.data_state == "stale" else ""
columns = st.columns(5)
columns[0].metric(f"Securities{suffix}", fmt_count(counts.get("securities")))
columns[1].metric(f"Price records{suffix}", fmt_count(counts.get("price_daily")))
columns[2].metric(f"Financial facts{suffix}", fmt_count(counts.get("financial_facts")))
columns[3].metric(f"Filings{suffix}", fmt_count(counts.get("filings")))
columns[4].metric(f"Last synchronization{suffix}", fmt_datetime(status.last_sync, missing="Never"))
if status.as_of:
    st.caption(f"Figures read at {fmt_datetime(status.as_of)}.")
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
    "Run OpenBB runtime check",
    help="Imports OpenBB V5 and lists the loaded providers. Takes several seconds.",
):
    with st.spinner("Importing OpenBB..."):
        st.session_state[RUNTIME_KEY] = run_openbb_runtime_check()
    st.rerun()
if openbb.runtime_checked:
    st.write(f"Providers: {', '.join(openbb.providers) or NA}")
    st.write(f"Routers: {', '.join(openbb.routers) or NA}")

st.subheader("Ollama")
ollama = status.ollama
st.markdown(status_text(ollama.available, ollama.detail))
st.caption(f"{ollama.base_url} - model: {ollama.configured_model}")
