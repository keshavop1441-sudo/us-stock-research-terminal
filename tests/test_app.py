"""Application startup, navigation and page behaviour via Streamlit's AppTest."""

import datetime as dt
import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.database import access
from app.models.records import PriceRecord, SecurityRecord
from app.services import status_service

PAGES = ["ui/home.py", "ui/screener.py", "ui/company.py", "ui/watchlists.py", "ui/data_status.py", "ui/settings.py"]
TIMEOUT = 30
# A closed port refuses at once on Linux but can time out on Windows; either way Ollama is reported unavailable.
OLLAMA_DOWN = re.compile(r"Is Ollama running\?|Timed out after")
ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "app" / "main.py"


@pytest.fixture(autouse=True)
def fresh_cache():
    status_service.forget_last_reading()
    yield
    status_service.forget_last_reading()


def start() -> AppTest:
    return AppTest.from_file(MAIN, default_timeout=TIMEOUT).run()


def find_button(at, label):
    return next(b for b in at.button if b.label == label)


def stored_queries(db_path):
    with access.reader(db_path) as r:
        return r.recent_queries()["query_text"].to_list()


def test_app_starts_and_creates_database(app_env):
    assert not app_env.exists()
    at = start()
    assert not at.exception
    assert [t.value for t in at.title] == ["US STOCK RESEARCH TERMINAL"]
    assert app_env.is_file()


def test_app_starts_without_ollama(app_env):
    at = start()
    assert not at.exception
    assert not at.error


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders(app_env, page):
    at = start()
    at.switch_page(page).run()
    assert not at.exception
    assert not at.error
    assert len(at.title) == 1


def test_invalid_configuration_shows_error_not_traceback(app_env, monkeypatch):
    monkeypatch.setenv("FAST_THINK", "maybe")
    at = start()
    assert not at.exception
    assert any("Invalid configuration" in e.value for e in at.error)


def test_home_page_content(app_env):
    at = start()
    assert at.text_area[0].label == "What are you looking for?"
    examples = [b.label for b in at.button if b.label != "Search"]
    assert "US technology companies with revenue growth above 15%." in examples
    assert "Profitable software companies with P/S below 5." in examples
    assert "Technology stocks down more than 30% from their 52-week high." in examples


def test_example_button_fills_search_box(app_env):
    at = start()
    find_button(at, "Profitable software companies with P/S below 5.").click().run()
    assert at.text_area[0].value == "Profitable software companies with P/S below 5."


def test_search_saves_query_and_says_it_is_not_implemented(app_env):
    at = start()
    at.text_area[0].set_value("US tech stocks down 30-50% from highs").run()
    find_button(at, "Search").click().run()
    assert not at.exception
    assert any("not implemented" in i.value for i in at.info)
    assert stored_queries(app_env) == ["US tech stocks down 30-50% from highs"]
    assert at.dataframe[0].value["query_text"].to_list() == ["US tech stocks down 30-50% from highs"]


def test_empty_search_is_rejected(app_env):
    at = start()
    find_button(at, "Search").click().run()
    assert any("Type a request" in w.value for w in at.warning)
    assert stored_queries(app_env) == []


def test_search_during_a_refresh_explains_and_saves_nothing(app_env, hold):
    start()  # creates the database
    with hold("writer"):
        at = start()
        at.text_area[0].set_value("tech stocks").run()
        find_button(at, "Search").click().run()
        assert not at.exception
        warnings = " ".join(w.value for w in at.warning)
        assert "could not be saved" in warnings and "being updated" in warnings
        assert not any("saved as query" in i.value for i in at.info)
    assert stored_queries(app_env) == []


def test_data_status_on_empty_database(app_env):
    at = start()
    at.switch_page("ui/data_status.py").run()
    assert not at.exception
    assert {m.label: m.value for m in at.metric} == {
        "Securities": "0",
        "Price records": "0",
        "Financial facts": "0",
        "Filings": "0",
        "Last synchronization": "Never",
    }
    text = " ".join(m.value for m in at.markdown)
    assert "Ready (schema v2)" in text
    assert OLLAMA_DOWN.search(text) and ":red[" in text
    assert str(app_env) in [t.value for t in at.text_input]


def test_data_status_when_database_file_is_missing(app_env):
    # Run the page on its own: going through main.py would re-create the database file.
    at = AppTest.from_file(ROOT / "app" / "ui" / "data_status.py", default_timeout=TIMEOUT).run()
    assert not at.exception
    assert {m.label: m.value for m in at.metric}["Securities"] == "N/A"
    assert any("Not created yet" in m.value for m in at.markdown)
    assert not app_env.exists()  # viewing status must not create the database


def test_data_status_during_refresh_shows_cached_figures_clearly_marked(app_env, hold):
    at = start()
    at.switch_page("ui/data_status.py").run()  # a live reading is taken (and cached) first
    with hold("writer"):
        at = start()
        at.switch_page("ui/data_status.py").run()
        assert not at.exception
        warnings = " ".join(w.value for w in at.warning)
        assert "refresh is in progress" in warnings and "cached figures" in warnings
        assert all(m.label.endswith("(cached)") for m in at.metric)
        assert any("Busy" in m.value for m in at.markdown)
    at.switch_page("ui/data_status.py").run()  # refresh over: live again, no cached marker
    assert not any(m.label.endswith("(cached)") for m in at.metric)


def test_data_status_during_refresh_without_earlier_reading_shows_na_not_zero(app_env, hold):
    start()
    status_service.forget_last_reading()
    with hold("writer"):
        at = start()
        at.switch_page("ui/data_status.py").run()
        assert not at.exception
        assert {m.label: m.value for m in at.metric}["Securities"] == "N/A"
        assert any("No earlier reading" in w.value for w in at.warning)


def test_settings_page_shows_configuration(app_env):
    at = start()
    at.switch_page("ui/settings.py").run()
    assert not at.exception
    values = {t.label: t.value for t in at.text_input}
    assert values["Ollama URL"] == "http://127.0.0.1:9"
    assert values["Ollama model"] == "qwen3.5:4b"
    assert {c.label: c.value for c in at.checkbox} == {"FAST_THINK": False, "DEEP_THINK": True}
    assert any(OLLAMA_DOWN.search(m.value) for m in at.markdown)


def lookup(at, text):
    at.text_input[0].set_value(text).run()
    find_button(at, "Look up").click().run()
    return at


def test_company_page_unknown_ticker(app_env):
    at = start()
    at.switch_page("ui/company.py").run()
    lookup(at, "ZZZZ")
    assert not at.exception
    assert any("not in the local database" in w.value for w in at.warning)


@pytest.mark.parametrize("bad", ["not a ticker!", "12345678901"])
def test_company_page_rejects_malformed_input_politely(app_env, bad):
    at = start()
    at.switch_page("ui/company.py").run()
    lookup(at, bad)
    assert not at.exception
    assert at.warning


@pytest.mark.parametrize("query", ["tst", "TST", "320193", "0000320193", "CIK320193"])
def test_company_page_with_stored_prices_by_ticker_or_any_cik_spelling(app_env, query):
    start()
    with access.writer(app_env, "test") as w:
        sid = w.upsert_security(SecurityRecord(ticker="TST", cik=320193, name="Test Corp"))
        w.upsert_prices(
            [
                PriceRecord(security_id=sid, trade_date=dt.date(2024, 1, 2), close=1.0),
                PriceRecord(security_id=sid, trade_date=dt.date(2024, 1, 3), close=2.0),
            ]
        )
    at = start()
    at.switch_page("ui/company.py").run()
    lookup(at, query)
    assert not at.exception
    assert [h.value for h in at.header] == ["TST - Test Corp"]
    assert {m.label: m.value for m in at.metric}["CIK"] == "0000320193"
    assert len(at.get("plotly_chart")) == 1


def test_company_page_without_prices_shows_na(app_env):
    start()
    with access.writer(app_env, "test") as w:
        w.upsert_security(SecurityRecord(ticker="TST"))
    at = start()
    at.switch_page("ui/company.py").run()
    lookup(at, "TST")
    assert not at.exception
    assert {m.label: m.value for m in at.metric}["CIK"] == "N/A"
    assert not at.get("plotly_chart")


def test_company_page_during_refresh_explains(app_env, hold):
    start()
    with hold("writer"):
        at = start()
        at.switch_page("ui/company.py").run()
        lookup(at, "AAPL")
        assert not at.exception
        assert any("unavailable" in w.value and "being updated" in w.value for w in at.warning)


def test_watchlist_can_be_created(app_env):
    at = start()
    at.switch_page("ui/watchlists.py").run()
    at.text_input[0].set_value("Growth").run()
    find_button(at, "Create watchlist").click().run()
    assert not at.exception
    assert any("Created watchlist 'Growth'" in s.value for s in at.success)
    with access.reader(app_env) as r:
        assert r.list_watchlists()["name"].to_list() == ["Growth"]
    at.text_input[0].set_value("growth").run()  # a duplicate name is refused, not crashed on
    find_button(at, "Create watchlist").click().run()
    assert any("already exists" in w.value for w in at.warning)


def test_watchlists_during_refresh_explain_instead_of_failing(app_env, hold):
    start()
    with hold("writer"):
        at = start()
        at.switch_page("ui/watchlists.py").run()
        assert not at.exception
        assert any("unavailable" in w.value for w in at.warning)
