"""Streamlit entry point. Run with: streamlit run app/main.py"""

import sys
from pathlib import Path

# Make the project root importable (Streamlit only puts app/ on sys.path).
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import streamlit as st

from app.services.errors import ConfigurationError
from app.services.settings_service import get_settings
from app.services.startup_service import prepare_database

st.set_page_config(
    page_title="US Stock Research Terminal",
    page_icon=":material/monitoring:",
    layout="wide",
)

try:
    get_settings()
except ConfigurationError as exc:
    st.error("Invalid configuration in the environment or .env file:")
    st.code(str(exc))
    st.stop()

startup = prepare_database()
if not startup.ready:
    st.warning(startup.message)

navigation = st.navigation(
    [
        st.Page("ui/home.py", title="Home", icon=":material/home:", default=True),
        st.Page("ui/screener.py", title="Screener", icon=":material/filter_alt:"),
        st.Page("ui/company.py", title="Company", icon=":material/business:"),
        st.Page("ui/watchlists.py", title="Watchlists", icon=":material/bookmarks:"),
        st.Page("ui/data_status.py", title="Data Status", icon=":material/database:"),
        st.Page("ui/settings.py", title="Settings", icon=":material/settings:"),
    ]
)
navigation.run()
