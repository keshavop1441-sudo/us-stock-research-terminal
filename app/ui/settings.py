import streamlit as st

from app.agent.llm_provider import get_provider
from app.config import DEFAULT_ENV_FILE
from app.ui.components import get_settings, status_text

settings = get_settings()

st.title("Settings")
st.caption(
    "Settings are read from environment variables and the .env file in the project folder "
    "(copy .env.example to .env). Edit the file and reload this page to change them."
)

st.subheader("Ollama")
st.text_input("Ollama URL", value=settings.ollama_base_url, disabled=True)
st.text_input("Ollama model", value=settings.ollama_model, disabled=True)
left, right = st.columns(2)
left.checkbox("FAST_THINK", value=settings.fast_think, disabled=True)
right.checkbox("DEEP_THINK", value=settings.deep_think, disabled=True)
st.caption(f"Effective reasoning mode: {settings.thinking_mode.value}")

st.subheader("Provider status")
provider = get_provider(settings).check_status()
st.markdown(status_text(provider.available, provider.detail))
if provider.installed_models:
    st.write("Installed models: " + ", ".join(provider.installed_models))
st.button("Refresh", icon=":material/refresh:")

st.subheader("Storage")
st.text_input("Database path", value=str(settings.resolved_database_path), disabled=True)
st.caption(
    f".env file: {'found' if DEFAULT_ENV_FILE.is_file() else 'not found (using defaults)'} at {DEFAULT_ENV_FILE}"
)
