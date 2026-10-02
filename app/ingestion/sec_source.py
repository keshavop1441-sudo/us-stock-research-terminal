"""Fetching raw SEC documents (no interpretation). Normalisation lives in ``sec_normalize``."""

from app.ingestion.retrieval import Retrieval
from app.ingestion.sec_http import SecHttpClient
from app.models.identifiers import normalize_cik
from app.models.sec_facts import COMPANYFACTS_URL

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


def fetch_ticker_map(client: SecHttpClient) -> Retrieval:
    return client.get_json(TICKER_MAP_URL, "company_tickers_exchange")


def fetch_submissions(client: SecHttpClient, cik: int | str) -> Retrieval:
    cik10 = normalize_cik(cik)
    return client.get_json(SUBMISSIONS_URL.format(cik=cik10), "submissions", {"cik": cik10})


def fetch_companyfacts(client: SecHttpClient, cik: int | str) -> Retrieval:
    cik10 = normalize_cik(cik)
    return client.get_json(COMPANYFACTS_URL.format(cik=cik10), "companyfacts", {"cik": cik10})
