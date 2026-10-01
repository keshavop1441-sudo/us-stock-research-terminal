"""Application startup, navigation and page behaviour via Streamlit's AppTest."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from app.database.connection import connect, init_database

PAGES = ["ui/home.py", "ui/screener.py", "ui/company.py", "ui/watchlists.py", "ui/data_status.py", "ui/settings.py"]
TIMEOUT = 30
MAIN = Path(__file__).resolve().parent.parent / "app" / "main.py"


def start() -> AppTest:
    return AppTest.from_file(MAIN, default_timeout=TIMEOUT).run()


def find_button(at, label):
    return next(b for b in at.button if b.label == label)


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
    with connect(app_env) as con:
        rows = con.execute("SELECT query_text, status FROM query_history").fetchall()
    assert rows == [("US tech stocks down 30-50% from highs", "received")]
    assert at.dataframe[0].value["query_text"].to_list() == ["US tech stocks down 30-50% from highs"]


def test_empty_search_is_rejected(app_env):
    at = start()
    find_button(at, "Search").click().run()
    assert any("Type a request" in w.value for w in at.warning)
    with connect(app_env) as con:
        assert con.execute("SELECT count(*) FROM query_history").fetchone()[0] == 0


def test_data_status_on_empty_database(app_env):
    at = start()
    at.switch_page("ui/data_status.py").run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics == {
        "Securities": "0",
        "Price records": "0",
        "Financial facts": "0",
        "Filings": "0",
        "Last synchronization": "Never",
    }
    text = " ".join(m.value for m in at.markdown)
    assert "Ready (schema v1)" in text
    assert "Is Ollama running?" in text
    assert str(app_env) in [t.value for t in at.text_input]


def test_data_status_when_database_file_is_missing(app_env):
    # Run the page on its own: going through main.py would re-create the database file.
    page = Path(__file__).resolve().parent.parent / "app" / "ui" / "data_status.py"
    at = AppTest.from_file(page, default_timeout=TIMEOUT).run()
    assert not at.exception
    assert {m.label: m.value for m in at.metric}["Securities"] == "N/A"
    assert any("Not created yet" in m.value for m in at.markdown)
    assert not app_env.exists()  # viewing status must not create the database


def test_settings_page_shows_configuration(app_env):
    at = start()
    at.switch_page("ui/settings.py").run()
    assert not at.exception
    values = {t.label: t.value for t in at.text_input}
    assert values["Ollama URL"] == "http://127.0.0.1:9"
    assert values["Ollama model"] == "qwen3.5:4b"
    assert {c.label: c.value for c in at.checkbox} == {"FAST_THINK": False, "DEEP_THINK": True}
    assert any("Is Ollama running?" in m.value for m in at.markdown)


def test_company_page_unknown_ticker(app_env):
    at = start()
    at.switch_page("ui/company.py").run()
    at.text_input[0].set_value("ZZZZ").run()
    find_button(at, "Look up").click().run()
    assert not at.exception
    assert any("not in the local database" in w.value for w in at.warning)


def test_company_page_with_stored_prices(app_env):
    init_database(app_env)
    with connect(app_env) as con:
        con.execute("INSERT INTO securities (cik, ticker, name) VALUES ('0000000001', 'TST', 'Test Corp')")
        con.execute(
            "INSERT INTO price_daily (security_id, trade_date, close) "
            "VALUES (1, '2024-01-02', 1.0), (1, '2024-01-03', 2.0)"
        )
    at = start()
    at.switch_page("ui/company.py").run()
    at.text_input[0].set_value("tst").run()
    find_button(at, "Look up").click().run()
    assert not at.exception
    assert [h.value for h in at.header] == ["TST - Test Corp"]
    assert {m.label: m.value for m in at.metric}["CIK"] == "0000000001"
    assert len(at.get("plotly_chart")) == 1


def test_company_page_without_prices_shows_na(app_env):
    init_database(app_env)
    with connect(app_env) as con:
        con.execute("INSERT INTO securities (ticker) VALUES ('TST')")
    at = start()
    at.switch_page("ui/company.py").run()
    at.text_input[0].set_value("TST").run()
    find_button(at, "Look up").click().run()
    assert not at.exception
    assert {m.label: m.value for m in at.metric}["CIK"] == "N/A"
    assert not at.get("plotly_chart")


def test_watchlist_can_be_created(app_env):
    at = start()
    at.switch_page("ui/watchlists.py").run()
    at.text_input[0].set_value("Growth").run()
    find_button(at, "Create watchlist").click().run()
    assert not at.exception
    assert any("Created watchlist 'Growth'" in s.value for s in at.success)
    with connect(app_env) as con:
        assert con.execute("SELECT name FROM watchlists").fetchall() == [("Growth",)]
    # a duplicate name is refused, not crashed on
    at.text_input[0].set_value("growth").run()
    find_button(at, "Create watchlist").click().run()
    assert any("already exists" in w.value for w in at.warning)
