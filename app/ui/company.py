import streamlit as st

from app.services import company_service
from app.services.errors import DataUnavailableError
from app.ui.charts import price_chart
from app.ui.components import NA, not_implemented

st.title("Company")

with st.form("company_lookup"):
    query = st.text_input("Ticker or CIK", placeholder="e.g. AAPL or 320193", max_chars=20)
    st.form_submit_button("Look up", type="primary")

query = query.strip()
if not query:
    st.caption("Enter a ticker or a CIK to open a company.")
    st.stop()

try:
    matches = company_service.find_companies(query)
    selected = None
    prices = None
    if matches.is_empty():
        st.warning(f"{query.upper()} is not in the local database ({NA}). No data has been synchronized for it.")
    else:
        labels = [
            f"{row['ticker']} - {row['name'] or NA} (CIK {row['cik'] or NA})" for row in matches.iter_rows(named=True)
        ]
        choice = st.selectbox("Security", labels) if len(labels) > 1 else labels[0]
        selected = matches.row(labels.index(choice), named=True)
        prices = company_service.price_history(selected["security_id"])
except ValueError as exc:  # malformed ticker / CIK
    st.warning(str(exc))
    st.stop()
except DataUnavailableError as exc:
    st.warning(f"Company data is unavailable: {exc}")
    st.stop()
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
        "Filings",
        ["10-K, 10-Q, 8-K list with links", "Section extraction (risk factors, MD&A, legal proceedings)"],
    )
with ownership:
    not_implemented("Ownership", ["Insider transactions", "Institutional holders (13F)", "Beneficial owners"])
with news:
    not_implemented(
        "News and events", ["Contracts, partnerships, acquisitions, lawsuits, regulatory items", "Recent news"]
    )
