import streamlit as st

from app.database import repository
from app.database.connection import connect
from app.ui.charts import price_chart
from app.ui.components import NA, get_settings, not_implemented

db_path = get_settings().resolved_database_path

st.title("Company")

with st.form("company_lookup"):
    ticker = st.text_input("Ticker", placeholder="e.g. AAPL", max_chars=12)
    st.form_submit_button("Look up", type="primary")

ticker = ticker.strip()
if not ticker:
    st.caption("Enter a ticker to open a company.")
    st.stop()

try:
    with connect(db_path) as con:
        matches = repository.find_securities(con, ticker)
        selected = None
        if matches.is_empty():
            st.warning(f"{ticker.upper()} is not in the local database ({NA}). No data has been synchronized for it.")
        else:
            labels = [
                f"{row['ticker']} - {row['name'] or NA} (CIK {row['cik'] or NA})"
                for row in matches.iter_rows(named=True)
            ]
            choice = st.selectbox("Security", labels) if len(labels) > 1 else labels[0]
            selected = matches.row(labels.index(choice), named=True)
        prices = repository.price_history(con, selected["security_id"]) if selected else None
except Exception as exc:  # noqa: BLE001
    st.error(f"Database error: {type(exc).__name__}: {exc}")
    st.stop()

if selected is None:
    st.stop()

st.header(f"{selected['ticker']} - {selected['name'] or NA}")
overview, financials, price, filings, ownership, news = st.tabs(
    ["Overview", "Financials", "Price", "Filings", "Ownership", "News & Events"]
)
with overview:
    left, middle, right = st.columns(3)
    left.metric("CIK", selected["cik"] or NA)
    middle.metric("Exchange", selected["exchange"] or NA)
    right.metric("Sector", selected["sector"] or NA)
    not_implemented(
        "The full company research page", ["Business summary", "Valuation and quality metrics", "Risk flags"]
    )
with financials:
    not_implemented(
        "Financial statements", ["Annual and quarterly statements from SEC facts", "Calculated growth and margins"]
    )
with price:
    if prices is None or prices.is_empty():
        st.info(f"Price history: {NA} (no daily prices stored for this security).")
    else:
        st.plotly_chart(price_chart(prices, f"{selected['ticker']} close"), width="stretch")
with filings:
    not_implemented(
        "Filings", ["10-K, 10-Q, 8-K list with links", "Section extraction (risk factors, MD&A, legal proceedings)"]
    )
with ownership:
    not_implemented("Ownership", ["Insider transactions", "Institutional holders (13F)", "Beneficial owners"])
with news:
    not_implemented(
        "News and events", ["Contracts, partnerships, acquisitions, lawsuits, regulatory items", "Recent news"]
    )
