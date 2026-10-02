"""READ-ONLY provider probe used by the Phase 2 data-source audit. NOT part of the application.

Purpose: find out empirically what the intended data sources really return, from an environment that is
allowed to reach them (the sandbox this project is developed in is not; GitHub-hosted runners are).
It issues a small number of polite GET/POST-search requests, never writes to the project database, and
prints one ``PROBE <json>`` line per probe so results can be read from a CI log.

    python scripts/audit/probe_providers.py --group connect_identity

Groups: connect_identity, market, fundamentals, ownership_events.  Set SEC_USER_AGENT to identify yourself
to the SEC (name + contact e-mail is what the SEC asks for); the default identifies this repository only.
"""

import argparse
import json
import os
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import httpx

REPO_UA = "us-stock-research-terminal-audit (https://github.com/keshavop1441-sudo/us-stock-research-terminal)"


def _openbb_default_ua() -> str:
    from openbb_sec.utils import definitions

    return definitions.SEC_HEADERS["User-Agent"]


# Stock behaviour first: unless SEC_USER_AGENT is set, use exactly the User-Agent OpenBB ships (a placeholder in
# the shape "name email"). A real deployment MUST identify itself with a real contact (SEC fair-access policy).
SEC_UA = os.environ.get("SEC_USER_AGENT") or _openbb_default_ua()
SEC_UA_MODE = "env SEC_USER_AGENT" if os.environ.get("SEC_USER_AGENT") else "openbb shipped default"
MAX_LINE = 9000

# ticker -> CIK as published by the SEC (verified against company_tickers.json in OpenBB's recorded fixture).
CORE = {"AAPL": 320193, "MSFT": 789019, "NVDA": 1045810, "AMZN": 1018724, "GOOGL": 1652044,
        "META": 1326801, "AMD": 2488, "AVGO": 1730168}  # fmt: skip
STRESS = {"RIVN": 1874178, "PTON": 1639825, "COST": 909832, "KOSS": 56701, "BRK-B": 1067983}
# Historical / discontinued identities: Twitter (acquired 2022), Activision (acquired 2023),
# Google Inc. (pre-Alphabet registrant, CIK differs from Alphabet's).
HISTORICAL = {"TWTR": 1418091, "ATVI": 718877, "GOOGLE_INC_OLD": 1288776}
ALL_CIKS = {**CORE, **STRESS, **HISTORICAL}

_results: list[dict] = []
_only: list = []  # compiled --only pattern (probes whose id does not match are skipped)


def emit(probe_id: str, ok: bool, started: float, info: object) -> None:
    record = {"id": probe_id, "ok": ok, "ms": round((time.monotonic() - started) * 1000), "info": info}
    line = json.dumps(record, default=str, ensure_ascii=True)
    if len(line) > MAX_LINE:
        line = json.dumps({**record, "info": {"truncated": True, "head": line[: MAX_LINE - 200]}}, default=str)
    print("PROBE " + line, flush=True)
    _results.append(record)


def probe(probe_id: str, fn, *args, **kwargs) -> object:
    """Run ``fn`` and record success/failure. Never raises."""
    if _only and not _only[0].search(probe_id):
        return None
    started = time.monotonic()
    try:
        info = fn(*args, **kwargs)
        emit(probe_id, True, started, info)
        return info
    except Exception as exc:  # noqa: BLE001 - an audit tool must record every failure mode
        emit(probe_id, False, started, {"error": type(exc).__name__, "message": str(exc)[:400],
                                        "trace_tail": traceback.format_exc().strip().splitlines()[-3:]})  # fmt: skip
        return None


# --- plain HTTP helpers (host reachability + endpoints OpenBB does not wrap) ------------------------------------


def http(method: str, url: str, *, ua: str = SEC_UA, sleep: float = 0.15, **kwargs) -> httpx.Response:
    time.sleep(sleep)  # SEC fair access: stay far below 10 requests/second
    headers = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate", **kwargs.pop("headers", {})}
    with httpx.Client(timeout=45, follow_redirects=True) as client:
        return client.request(method, url, headers=headers, **kwargs)


def reach(url: str, **kwargs) -> dict:
    response = http("GET", url, **kwargs)
    ctype = response.headers.get("content-type", "")
    return {
        "status": response.status_code, "content_type": ctype, "bytes": len(response.content),
        "date_header": response.headers.get("date"), "server": response.headers.get("server"),
        "head": response.text[:160].replace("\n", " "),
    }  # fmt: skip


def sec_json(url: str) -> dict:
    response = http("GET", url)
    response.raise_for_status()
    return response.json()


def cik10(cik: int) -> str:
    return f"{cik:010d}"


# --- OpenBB helpers ---------------------------------------------------------------------------------------------


def use_obb():
    from openbb import obb
    from openbb_sec.utils import definitions

    if os.environ.get("SEC_USER_AGENT"):
        for headers in (definitions.SEC_HEADERS, definitions.HEADERS):  # override OpenBB's placeholder in place
            headers["User-Agent"] = SEC_UA
    return obb


def rows(result) -> list[dict]:
    """Normalise an OpenBB result to a list of JSON dicts (some commands return one model, not a list)."""
    data = result.results
    if not isinstance(data, list):
        data = [data]
    return [r.model_dump(mode="json") if hasattr(r, "model_dump") else r for r in data]


def nonnull(records: list[dict]) -> dict[str, int]:
    counts: Counter = Counter()
    for record in records:
        counts.update(k for k, v in record.items() if v is not None)
    return dict(counts)


def pick(record: dict, fields: list[str]) -> dict:
    return {f: record.get(f) for f in fields}


def summary(result, fields: list[str], n: int = 3, tail: bool = False) -> dict:
    records = rows(result)
    shown = records[-n:] if tail else records[:n]
    extra = getattr(result, "extra", {}) or {}
    return {
        "n_rows": len(records), "n_columns": len(records[0]) if records else 0,
        "columns_all_null": sorted(set(records[0]) - set(nonnull(records))) if records else [],
        "rows": [pick(r, fields) for r in shown], "extra_keys": sorted(extra)[:6],
    }  # fmt: skip


def today() -> date:
    return datetime.now(UTC).date()


# =====================================================================================================================
# group: connect_identity
# =====================================================================================================================


def group_connect_identity() -> None:
    obb = use_obb()
    print("PROBE_ENV " + json.dumps({"utc_now": datetime.now(UTC).isoformat(), "python": sys.version.split()[0],
                                     "sec_ua_mode": SEC_UA_MODE}))  # fmt: skip
    from openbb_nasdaq.utils.helpers import get_headers

    hosts = {
        "sec_company_tickers": "https://www.sec.gov/files/company_tickers.json",
        "sec_company_tickers_exchange": "https://www.sec.gov/files/company_tickers_exchange.json",
        "sec_submissions": f"https://data.sec.gov/submissions/CIK{cik10(320193)}.json",
        "sec_companyfacts": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10(320193)}.json",
        "sec_frames": "https://data.sec.gov/api/xbrl/frames/us-gaap/Revenues/USD/CY2023.json",
        "sec_efts_fulltext": "https://efts.sec.gov/LATEST/search-index?q=%22share%20repurchase%22&forms=8-K",
        "sec_edgar_archives": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0000320193&type=10-K&output=atom",
        "cboe_delayed_quote": "https://cdn.cboe.com/api/global/delayed_quotes/quotes/AAPL.json",
        "cboe_history": "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/AAPL.json",
        "usaspending_agencies": "https://api.usaspending.gov/api/v2/references/toptier_agencies/",
        "google_news_rss": "https://news.google.com/rss/search?q=AAPL+stock&hl=en-US&gl=US&ceid=US:en",
        "yahoo_finance_rss": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL&region=US&lang=en-US",
    }
    for name, url in hosts.items():
        probe(f"host.{name}", reach, url)
    probe("host.nasdaq_api_quote", reach, "https://api.nasdaq.com/api/quote/AAPL/info?assetclass=stocks",
          ua=get_headers()["User-Agent"], headers={k: v for k, v in get_headers().items() if k != "User-Agent"})  # fmt: skip

    def ua_experiment() -> dict:
        url = f"https://data.sec.gov/submissions/CIK{cik10(320193)}.json"
        out = {}
        browser = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        for label, ua in (
            ("repo_id_no_email", REPO_UA),
            ("openbb_shipped_default", _openbb_default_ua()),
            ("browser_like", browser),
        ):
            r = http("GET", url, ua=ua, sleep=1.0)
            out[label] = {
                "ua": ua,
                "status": r.status_code,
                "bytes": len(r.content),
                "head": r.text[:120].replace("\n", " "),
            }
        return out

    probe("sec.user_agent_experiment", ua_experiment)

    # --- SEC reference data ---------------------------------------------------------------------------------------
    def tickers_exchange() -> dict:
        data = sec_json("https://www.sec.gov/files/company_tickers_exchange.json")
        fields, body = data["fields"], data["data"]
        index = {}
        for row in body:
            index.setdefault(dict(zip(fields, row, strict=True))["ticker"], []).append(
                dict(zip(fields, row, strict=True))
            )
        wanted = [
            "AAPL",
            "GOOGL",
            "GOOG",
            "BRK-B",
            "BRK.B",
            "BRK-A",
            "META",
            "FB",
            "NVDA",
            "TWTR",
            "ATVI",
            "RIVN",
            "PTON",
            "COST",
            "KOSS",
        ]
        return {"fields": fields, "n": len(body), "lookup": {t: index.get(t) for t in wanted},
                "exchanges": dict(Counter(dict(zip(fields, r, strict=True))["exchange"] for r in body).most_common(8))}  # fmt: skip

    probe("sec.company_tickers_exchange", tickers_exchange)

    def submissions(label: str, cik: int) -> dict:
        data = sec_json(f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json")
        recent = data["filings"]["recent"]
        forms = Counter(recent["form"])
        cutoff = (today() - timedelta(days=730)).isoformat()
        amendments = [(f, d, a) for f, d, a in zip(recent["form"], recent["filingDate"], recent["accessionNumber"], strict=True)
                      if f.endswith("/A") and d >= cutoff]  # fmt: skip
        items_2y = Counter(i for f, d, items in zip(recent["form"], recent["filingDate"], recent["items"], strict=True)
                           if f == "8-K" and d >= cutoff for i in items.split(",") if i)  # fmt: skip
        return {
            "label": label, "name": data["name"], "cik": data["cik"], "tickers": data["tickers"],
            "exchanges": data["exchanges"], "sic": data["sic"], "sicDescription": data["sicDescription"],
            "fiscalYearEnd": data["fiscalYearEnd"], "entityType": data["entityType"], "category": data.get("category"),
            "formerNames": data["formerNames"][:5], "n_recent": len(recent["form"]),
            "recent_span": [recent["filingDate"][-1], recent["filingDate"][0]],
            "older_pages": [(f["name"], f["filingFrom"], f["filingTo"]) for f in data["filings"]["files"]],
            "forms_top": forms.most_common(8), "amendments_2y": amendments[:6], "n_8k_items_2y": dict(items_2y.most_common(12)),
        }  # fmt: skip

    for label, cik in ALL_CIKS.items():
        probe(f"sec.submissions.{label}", submissions, label, cik)

    # --- the same reference data through OpenBB V5 --------------------------------------------------------------
    for symbol in ["AAPL", "BRK-B", "BRK.B", "BRKB", "GOOG", "META", "FB", "TWTR", "ATVI"]:
        probe(f"obb.sec.cik_map.{symbol}", lambda s=symbol: rows(obb.sec.cik_map(symbol=s, provider="sec")))
    for label, cik in {
        "META": 1326801,
        "GOOGL": 1652044,
        "OLD_GOOGLE": 1288776,
        "BRKB": 1067983,
        "TWTR": 1418091,
    }.items():
        probe(f"obb.sec.symbol_map.{label}", lambda c=cik: rows(obb.sec.symbol_map(query=str(c), provider="sec")))
    for query in ["BRK", "alphabet", "facebook", "twitter"]:
        probe(f"obb.sec.equity_search.{query}", lambda q=query: summary(obb.sec.equity_search(query=q, provider="sec"), ["symbol", "name", "cik"], 6))  # fmt: skip

    def companyfacts_dei(label: str, cik: int) -> dict:
        data = sec_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10(cik)}.json")
        facts = data["facts"]
        dei = facts.get("dei", {})
        concept = dei.get("EntityCommonStockSharesOutstanding", {})
        points = [p for unit in concept.get("units", {}).values() for p in unit]
        return {"label": label, "taxonomies": {k: len(v) for k, v in facts.items()},
                "dei_concepts": sorted(dei)[:12], "has_TradingSymbol": "TradingSymbol" in dei,
                "shares_outstanding_points": len(points), "shares_outstanding_last": points[-2:]}  # fmt: skip

    for label in ("META", "GOOGL", "BRK-B"):
        probe(f"sec.companyfacts_dei.{label}", companyfacts_dei, label, ALL_CIKS[label])

    def symbol_directory() -> dict:
        out = {}
        wanted = {
            "AAPL",
            "GOOGL",
            "GOOG",
            "BRK.B",
            "BRK.A",
            "META",
            "NVDA",
            "RIVN",
            "PTON",
            "COST",
            "KOSS",
            "SPY",
            "BRKB",
        }
        for name in ("nasdaqlisted", "otherlisted"):
            response = http(
                "GET",
                f"https://www.nasdaqtrader.com/dynamic/SymDir/{name}.txt",
                ua="Mozilla/5.0 (compatible; audit-probe)",
            )
            lines = response.text.strip().splitlines()
            header = lines[0].split("|")
            records = [dict(zip(header, line.split("|"), strict=False)) for line in lines[1:-1]]
            key = "Symbol" if "Symbol" in header else "ACT Symbol"
            out[name] = {"status": response.status_code, "header": header, "n": len(records), "footer": lines[-1][:80],
                         "rows": {r[key]: r for r in records if r.get(key) in wanted},
                         "etf_flag_counts": dict(Counter(r.get("ETF") for r in records)), "test_issue_counts": dict(Counter(r.get("Test Issue") for r in records)),
                         "exchange_counts": dict(Counter(r.get("Exchange") for r in records)) if name == "otherlisted" else None,
                         "market_category": dict(Counter(r.get("Market Category") for r in records)) if name == "nasdaqlisted" else None}  # fmt: skip
        return out

    probe("nasdaqtrader.symbol_directory", symbol_directory)
    probe("obb.nasdaq.screener.tech_mega", lambda: summary(obb.nasdaq.equity.screener(exchange="nasdaq", sector="technology", mktcap="mega", provider="nasdaq"), ["symbol", "name", "last_price", "market_cap", "country", "ipo_year", "industry", "sector"], 5))  # fmt: skip
    probe("obb.nasdaq.calendar_splits.2024-06", lambda: summary(obb.nasdaq.equity.calendar.splits(start_date="2024-06-01", end_date="2024-06-30", provider="nasdaq"), ["date", "symbol", "numerator", "denominator", "ratio_display", "payable_date"], 8))  # fmt: skip

    # --- Nasdaq / Cboe identity ---------------------------------------------------------------------------------------
    profile_fields = ["symbol", "name", "cik", "cusip", "isin", "stock_exchange", "sic", "sector", "industry_category",
                      "entity_status", "stock_type", "exchange", "inc_state", "first_stock_price_date", "last_stock_price_date"]  # fmt: skip
    for symbol in ["AAPL", "META", "GOOGL", "BRK/B", "BRK.B", "RIVN", "TWTR"]:
        probe(f"obb.nasdaq.profile.{symbol}", lambda s=symbol: summary(obb.nasdaq.equity.profile(symbol=s, provider="nasdaq"), profile_fields, 1))  # fmt: skip
    for query in ["BRK", "GOOG", "META"]:
        probe(f"obb.nasdaq.search.{query}", lambda q=query: summary(obb.nasdaq.equity.search(query=q, provider="nasdaq"), ["symbol", "name", "exchange", "etf", "financial_status", "test_issue"], 6))  # fmt: skip
        probe(
            f"obb.cboe.search.{query}",
            lambda q=query: summary(obb.cboe.equity.search(query=q, provider="cboe"), ["symbol", "name"], 6),
        )


# =====================================================================================================================
# group: market
# =====================================================================================================================


def history_rows(fn, symbol: str, start: str, end: str, provider: str) -> list[dict]:
    return rows(fn(symbol=symbol, start_date=start, end_date=end, provider=provider))


def group_market() -> None:
    obb = use_obb()
    windows = {  # split effective dates: AAPL 4:1 2020-08-31, AMZN 20:1 2022-06-06, GOOGL 20:1 2022-07-18, NVDA 10:1 2024-06-10
        "AAPL": ("2020-08-26", "2020-09-02"), "AMZN": ("2022-06-01", "2022-06-08"),
        "GOOGL": ("2022-07-13", "2022-07-20"), "NVDA": ("2024-06-04", "2024-06-12"),
    }  # fmt: skip
    nasdaq_full: dict[str, list[dict]] = {}

    def nasdaq_slice(symbol: str, start: str, end: str) -> list[dict]:
        if symbol not in nasdaq_full:
            nasdaq_full[symbol] = history_rows(
                obb.nasdaq.equity.historical, symbol, "2000-01-01", today().isoformat(), "nasdaq"
            )
        return [
            pick(r, ["date", "open", "close", "volume"])
            for r in nasdaq_full[symbol]
            if start <= str(r["date"])[:10] <= end
        ]

    for symbol, (start, end) in windows.items():
        probe(f"split.cboe.{symbol}", lambda s=symbol, a=start, b=end: [pick(r, ["date", "open", "close", "volume"]) for r in history_rows(obb.cboe.equity.historical, s, a, b, "cboe")])  # fmt: skip
        probe(f"split.nasdaq_full_range_sliced.{symbol}", nasdaq_slice, symbol, start, end)
        probe(f"split.nasdaq_narrow_window.{symbol}", lambda s=symbol, a=start, b=end: [pick(r, ["date", "close"]) for r in history_rows(obb.nasdaq.equity.historical, s, a, b, "nasdaq")])  # fmt: skip

    # dividend adjustment: compare the SAME historical dates across providers for dividend payers
    for symbol in ("MSFT", "KO"):
        probe(f"divadj.cboe.{symbol}", lambda s=symbol: [pick(r, ["date", "close"]) for r in history_rows(obb.cboe.equity.historical, s, "2015-01-02", "2015-01-07", "cboe")])  # fmt: skip
        probe(f"divadj.nasdaq_sliced.{symbol}", lambda s=symbol: [{"date": r["date"], "close": r["close"]} for r in nasdaq_slice(s, "2016-10-03", "2016-10-07")])  # fmt: skip
        probe(f"divadj.cboe_same_dates.{symbol}", lambda s=symbol: [pick(r, ["date", "close"]) for r in history_rows(obb.cboe.equity.historical, s, "2016-10-03", "2016-10-07", "cboe")])  # fmt: skip

    def depth(provider: str, fn, symbol: str) -> dict:
        data = history_rows(fn, symbol, "1990-01-01", today().isoformat(), provider)
        dates = [r["date"] for r in data]
        gaps = [(a, b) for a, b in zip(dates, dates[1:], strict=False)
                if (date.fromisoformat(str(b)[:10]) - date.fromisoformat(str(a)[:10])).days > 4][-8:]  # fmt: skip
        return {"n": len(data), "first": dates[:1], "last": dates[-2:], "gaps_over_4_days_recent": gaps,
                "date_type": type(data[0]["date"]).__name__ if data else None, "columns": sorted(data[0]) if data else []}  # fmt: skip

    for provider, fn in (("cboe", obb.cboe.equity.historical), ("nasdaq", obb.nasdaq.equity.historical)):
        probe(f"depth.{provider}.AAPL", depth, provider, fn, "AAPL")
        probe(f"depth.{provider}.RIVN", depth, provider, fn, "RIVN")  # recent IPO: short history

    # benchmarks and sector proxies
    for symbol in ("SPY", "QQQ", "XLK"):
        probe(f"bench.cboe.{symbol}", lambda s=symbol: summary(obb.cboe.equity.historical(symbol=s, start_date=(today() - timedelta(days=10)).isoformat(), provider="cboe"), ["date", "close", "volume"], 3, tail=True))  # fmt: skip
    probe("bench.cboe.index.SPX", lambda: summary(obb.cboe.index.historical(symbol="SPX", start_date=(today() - timedelta(days=10)).isoformat(), provider="cboe"), ["date", "close"], 3, tail=True))  # fmt: skip
    probe("bench.nasdaq.index.COMP", lambda: summary(obb.nasdaq.index.historical(symbol="COMP", start_date=(today() - timedelta(days=10)).isoformat(), provider="nasdaq"), ["date", "close"], 3, tail=True))  # fmt: skip

    # quotes: delayed/live semantics, 52-week fields, market cap, timestamps
    quote_fields = ["symbol", "name", "exchange", "last_price", "prev_close", "open", "high", "low", "volume", "year_high", "year_low",
                    "market_cap", "average_volume", "last_timestamp", "market_status", "sector", "industry", "change_percent"]  # fmt: skip
    for symbol in ("AAPL", "NVDA", "RIVN", "BRK/B", "BRK.B"):
        probe(f"quote.nasdaq.{symbol}", lambda s=symbol: summary(obb.nasdaq.equity.quote(symbol=s, provider="nasdaq"), quote_fields, 1))  # fmt: skip
    for symbol in ("AAPL", "NVDA", "BRK.B", "BRK-B"):
        probe(f"quote.cboe.{symbol}", lambda s=symbol: summary(obb.cboe.equity.quote(symbol=s, provider="cboe"), quote_fields + ["iv30"], 1))  # fmt: skip
    probe("market.nasdaq.status", lambda: rows(obb.nasdaq.markets.status(provider="nasdaq")))

    # 52-week values: quote vs calculated from daily history (close-based and high/low-based)
    def week52(symbol: str) -> dict:
        start = (today() - timedelta(days=400)).isoformat()
        hist = history_rows(obb.cboe.equity.historical, symbol, start, today().isoformat(), "cboe")
        cutoff = (today() - timedelta(days=365)).isoformat()
        window = [r for r in hist if str(r["date"])[:10] >= cutoff]
        quote = rows(obb.nasdaq.equity.quote(symbol=symbol, provider="nasdaq"))[0]
        last_close = window[-1]["close"]
        return {
            "n_window": len(window), "first": str(window[0]["date"])[:10], "last": str(window[-1]["date"])[:10],
            "calc_high_by_high": max(r["high"] for r in window), "calc_high_by_close": max(r["close"] for r in window),
            "calc_low_by_low": min(r["low"] for r in window), "calc_low_by_close": min(r["close"] for r in window),
            "last_close": last_close, "quote_year_high": quote.get("year_high"), "quote_year_low": quote.get("year_low"),
            "quote_last_price": quote.get("last_price"), "quote_market_cap": quote.get("market_cap"),
        }  # fmt: skip

    for symbol in ("AAPL", "NVDA", "MSFT"):
        probe(f"week52.{symbol}", week52, symbol)


# =====================================================================================================================
# group: fundamentals
# =====================================================================================================================

IS_FIELDS = ["period_ending", "fiscal_period", "fiscal_year", "total_revenue", "total_cost_of_revenue", "total_gross_profit",
             "total_operating_income", "total_pretax_income", "net_income", "net_income_to_common", "basic_eps", "diluted_eps",
             "weighted_ave_diluted_shares_os"]  # fmt: skip
BS_FIELDS = ["period_ending", "fiscal_period", "fiscal_year", "cash_and_equivalents", "short_term_investments", "total_assets",
             "total_liabilities", "short_term_debt", "current_portion_of_long_term_debt", "long_term_debt", "total_equity",
             "total_common_equity", "total_liabilities_and_shareholders_equity"]  # fmt: skip
CF_FIELDS = ["period_ending", "fiscal_period", "fiscal_year", "net_cash_from_operating_activities",
             "purchase_of_plant_property_and_equipment", "net_cash_from_investing_activities", "net_cash_from_financing_activities",
             "net_change_in_cash"]  # fmt: skip


def group_fundamentals() -> None:
    obb = use_obb()
    symbols = ["AAPL", "MSFT", "NVDA", "RIVN", "PTON", "COST", "KOSS", "GOOGL", "BRK-B"]
    for symbol in symbols:
        for period in ("annual", "quarterly", "ttm"):
            probe(f"sec.income.{symbol}.{period}", lambda s=symbol, p=period: summary(
                obb.sec.income_statement(symbol=s, period=p, limit=4, provider="sec"), IS_FIELDS, 4))  # fmt: skip
    for symbol in ("AAPL", "RIVN", "KOSS", "BRK-B"):
        probe(f"sec.balance.{symbol}.annual", lambda s=symbol: summary(obb.sec.balance_sheet(symbol=s, period="annual", limit=3, provider="sec"), BS_FIELDS, 3))  # fmt: skip
        probe(f"sec.cashflow.{symbol}.annual", lambda s=symbol: summary(obb.sec.cash_flow(symbol=s, period="annual", limit=3, provider="sec"), CF_FIELDS, 3))  # fmt: skip
        probe(f"sec.cashflow.{symbol}.ttm", lambda s=symbol: summary(obb.sec.cash_flow(symbol=s, period="ttm", limit=2, provider="sec"), CF_FIELDS, 2))  # fmt: skip
    probe("sec.balance.AAPL.quarterly", lambda: summary(obb.sec.balance_sheet(symbol="AAPL", period="quarterly", limit=4, provider="sec"), BS_FIELDS, 4))  # fmt: skip

    # point-in-time mode vs default (restated) for the same periods
    for symbol in ("AAPL", "MSFT"):
        for pit in (False, True):
            probe(f"sec.income.{symbol}.annual.pit_{pit}", lambda s=symbol, p=pit: summary(
                obb.sec.income_statement(symbol=s, period="annual", limit=5, pit_mode=p, provider="sec"), IS_FIELDS, 5))  # fmt: skip
    probe("sec.income.AAPL.quarterly.preliminary", lambda: summary(
        obb.sec.income_statement(symbol="AAPL", period="quarterly", limit=3, include_preliminary=True, provider="sec"), IS_FIELDS, 3))  # fmt: skip

    def field_provenance(symbol: str) -> dict:
        result = obb.sec.income_statement(symbol=symbol, period="annual", limit=2, provider="sec")
        meta = (getattr(result, "extra", {}) or {}).get("results_metadata") or {}
        fields = meta.get("fields", {}) if isinstance(meta, dict) else {}
        wanted = ["total_revenue", "total_gross_profit", "total_operating_income", "net_income", "diluted_eps"]
        return {"meta_keys": sorted(meta)[:8] if isinstance(meta, dict) else str(type(meta)),
                "entity": meta.get("entity_name") if isinstance(meta, dict) else None,
                "company_type": meta.get("company_type") if isinstance(meta, dict) else None,
                "field_sources": {k: (fields.get(k) or {}).get("sources") for k in wanted},
                "validation_warnings": (meta.get("validation_warnings") or [])[:3] if isinstance(meta, dict) else None}  # fmt: skip

    for symbol in ("AAPL", "RIVN", "KOSS"):
        probe(f"sec.income.provenance.{symbol}", field_provenance, symbol)

    # Raw XBRL company facts: filing date / accession / restatement behaviour that OpenBB's wide rows hide
    def raw_facts(label: str, cik: int, tags: dict[str, list[str]]) -> dict:
        data = sec_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10(cik)}.json")["facts"]["us-gaap"]
        out: dict = {"label": label}
        for name, candidates in tags.items():
            tag = next((t for t in candidates if t in data), None)
            if tag is None:
                out[name] = {"tag": None, "note": f"none of {candidates} present"}
                continue
            units = data[tag]["units"]
            unit = "USD" if "USD" in units else next(iter(units))
            points = units[unit]
            by_period: dict = {}
            for p in points:
                by_period.setdefault((p.get("start"), p["end"]), []).append(p)
            restated = {k: v for k, v in by_period.items() if len({x["val"] for x in v}) > 1}
            sample = sorted(restated.items(), key=lambda kv: kv[0][1])[-1:] if restated else []
            out[name] = {
                "tag": tag, "unit": unit, "n_points": len(points), "keys": sorted(points[0]),
                "forms": dict(Counter(p["form"] for p in points).most_common(6)),
                "periods_with_multiple_values": len(restated),
                "restatement_example": [{"period": k, "values": [(x["val"], x["accn"], x["form"], x["filed"], x.get("fy"), x.get("fp")) for x in v]}
                                        for k, v in sample],  # fmt: skip
                "latest": [(p.get("start"), p["end"], p["val"], p["form"], p["filed"], p.get("fy"), p.get("fp"), p.get("frame")) for p in points[-3:]],
            }  # fmt: skip
        return out

    revenue_tags = {"revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
                    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"], "op_cash_flow": ["NetCashProvidedByUsedInOperatingActivities"],
                    "eps_diluted": ["EarningsPerShareDiluted"], "net_income": ["NetIncomeLoss"], "debt_lt": ["LongTermDebtNoncurrent", "LongTermDebt"]}  # fmt: skip
    for label in ("AAPL", "RIVN", "KOSS"):
        probe(f"sec.raw_facts.{label}", raw_facts, label, ALL_CIKS[label], revenue_tags)

    # Nasdaq published statements (fallback comparison): depth and fields
    probe("nasdaq.income.AAPL.annual", lambda: summary(obb.nasdaq.equity.fundamental.income(symbol="AAPL", period="annual", provider="nasdaq"), ["period_ending", "fiscal_year", "revenue", "gross_profit", "operating_income", "net_income"], 6))  # fmt: skip
    probe("nasdaq.income.AAPL.quarter", lambda: summary(obb.nasdaq.equity.fundamental.income(symbol="AAPL", period="quarter", provider="nasdaq"), ["period_ending", "fiscal_period", "revenue", "net_income"], 6))  # fmt: skip
    probe("nasdaq.balance.AAPL", lambda: summary(obb.nasdaq.equity.fundamental.balance(symbol="AAPL", provider="nasdaq"), ["period_ending", "fiscal_year"], 2))  # fmt: skip
    probe("nasdaq.cash.AAPL", lambda: summary(obb.nasdaq.equity.fundamental.cash(symbol="AAPL", provider="nasdaq"), ["period_ending", "fiscal_year"], 2))  # fmt: skip
    probe("nasdaq.ratios.AAPL", lambda: summary(obb.nasdaq.equity.fundamental.ratios(symbol="AAPL", provider="nasdaq"), ["period_ending", "gross_profit_margin", "net_profit_margin", "current_ratio"], 3))  # fmt: skip

    # frames: cross-sectional pull (one concept, all companies, one calendar period) - basis for market-wide screens
    def frames() -> dict:
        data = sec_json(
            "https://data.sec.gov/api/xbrl/frames/us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax/USD/CY2023.json"
        )
        by_cik = {r["cik"]: r for r in data["data"]}
        return {"n": data["pts"], "keys": sorted(data["data"][0]), "ccp": data["ccp"], "uom": data["uom"],
                "AAPL": by_cik.get(320193), "NVDA": by_cik.get(1045810), "RIVN": by_cik.get(1874178), "KOSS": by_cik.get(56701)}  # fmt: skip

    probe("sec.frames.revenue_CY2023", frames)


# =====================================================================================================================
# group: ownership_events
# =====================================================================================================================


def group_ownership_events() -> None:
    obb = use_obb()
    # --- filings -----------------------------------------------------------------------------------------------------
    filing_fields = ["filing_date", "report_date", "report_type", "accession_number", "primary_doc", "primary_doc_description", "items", "is_xbrl", "report_url"]  # fmt: skip
    for form in ("10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "4", "3", "5", "13F-HR"):
        symbol = "BRK-B" if form == "13F-HR" else "AAPL"
        probe(f"sec.filings.{symbol}.{form}", lambda s=symbol, f=form: summary(obb.sec.company_filings(symbol=s, form_type=f, limit=3, provider="sec"), filing_fields, 3))  # fmt: skip
    probe("sec.filings.by_cik.META", lambda: summary(obb.sec.company_filings(symbol=None, cik=cik10(1326801), form_type="10-K", limit=3, provider="sec"), filing_fields, 3))  # fmt: skip
    probe(
        "sec.filings.cik_as_int.META(expected: TypeError in OpenBB)",
        lambda: summary(
            obb.sec.company_filings(symbol=None, cik=1326801, form_type="10-K", limit=1, provider="sec"),
            filing_fields,
            1,
        ),
    )
    probe("sec.filings.by_symbol.META", lambda: summary(obb.sec.company_filings(symbol="META", form_type="10-K", limit=3, provider="sec"), filing_fields, 3))  # fmt: skip

    def amendments_client_side() -> dict:
        res = obb.sec.company_filings(symbol="AAPL", limit=400, provider="sec")
        records = rows(res)
        return {"n": len(records), "forms": dict(Counter(r.get("report_type") for r in records).most_common(10)),
                "amended": [pick(r, ["filing_date", "report_type", "accession_number"]) for r in records if str(r.get("report_type", "")).endswith("/A")][:5]}  # fmt: skip

    probe("sec.filings.AAPL.client_side_amendment_filter", amendments_client_side)
    probe("sec.filings.old_google_cik", lambda: summary(obb.sec.company_filings(symbol=None, cik=cik10(1288776), form_type="10-K", limit=3, provider="sec"), filing_fields, 3))  # fmt: skip
    probe("sec.filings.TWTR_cik", lambda: summary(obb.sec.company_filings(symbol=None, cik=cik10(1418091), limit=3, provider="sec"), filing_fields, 3))  # fmt: skip
    probe("nasdaq.filings.AAPL.8k", lambda: summary(obb.nasdaq.equity.filings(symbol="AAPL", provider="nasdaq"), ["filing_date", "report_type", "period_ending", "report_url", "doc_link"], 3))  # fmt: skip

    # amendments: scan submissions for recent 10-K/A, 10-Q/A across candidates (empirical choice of a test company)
    def amendment_scan() -> dict:
        found = {}
        for label, cik in {**CORE, **STRESS, "TSLA": 1318605, "INTC": 50863, "BA": 12927, "NKE": 320187, "ORCL": 1341439,
                           "PFE": 78003, "F": 37996, "UBER": 1543151, "SNAP": 1564408, "PLTR": 1321655, "COIN": 1679788}.items():  # fmt: skip
            recent = sec_json(f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json")["filings"]["recent"]
            cutoff = (today() - timedelta(days=900)).isoformat()
            hits = [(f, d, a) for f, d, a in zip(recent["form"], recent["filingDate"], recent["accessionNumber"], strict=True)
                    if f in ("10-K/A", "10-Q/A") and d >= cutoff]  # fmt: skip
            if hits:
                found[label] = hits[:3]
        return found

    probe("sec.amendment_scan", amendment_scan)

    # --- insider activity -------------------------------------------------------------------------------------------
    insider_fields = ["filing_date", "transaction_date", "owner_name", "owner_title", "form", "transaction_type", "acquisition_or_disposition",
                      "security_type", "securities_transacted", "transaction_price", "securities_owned", "ownership_type", "transaction_value",
                      "underlying_security_title", "filing_url"]  # fmt: skip

    def insider(symbol: str, provider: str, limit: int) -> dict:
        fn = obb.sec.insider_trading if provider == "sec" else obb.nasdaq.equity.ownership.insider_trading
        res = fn(symbol=symbol, limit=limit, provider=provider)
        records = rows(res)
        base = summary(res, insider_fields, 4)
        base["transaction_codes"] = dict(Counter(r.get("transaction_type") for r in records).most_common(12))
        base["derivative_rows"] = sum(1 for r in records if r.get("underlying_security_title"))
        base["filing_vs_transaction_lag_examples"] = [
            (r.get("transaction_date"), r.get("filing_date")) for r in records[:4]
        ]
        return base

    probe("insider.sec.AAPL", insider, "AAPL", "sec", 15)
    probe("insider.nasdaq.AAPL", insider, "AAPL", "nasdaq", 40)
    probe("insider.sec.NVDA", insider, "NVDA", "sec", 15)

    # --- institutional ----------------------------------------------------------------------------------------------
    def institutional(symbol: str, holder_type: str) -> dict:
        res = obb.nasdaq.equity.ownership.institutional(
            symbol=symbol, limit=50, holder_type=holder_type, provider="nasdaq"
        )
        records = rows(res)
        base = summary(
            res, ["owner_name", "date", "shares_held", "shares_change", "shares_change_percent", "market_value"], 3
        )
        base["distinct_report_dates"] = dict(Counter(str(r.get("date"))[:10] for r in records).most_common(6))
        return base

    for holder_type in ("all", "new", "increased", "decreased", "sold_out"):
        probe(f"institutional.nasdaq.AAPL.{holder_type}", institutional, "AAPL", holder_type)

    def form13f(symbol: str) -> dict:
        res = obb.sec.form_13f(symbol=symbol, limit=1, provider="sec")
        records = rows(res)
        return {"n_holdings": len(records), "period_ending": sorted({str(r["period_ending"]) for r in records}),
                "sample": [pick(r, ["issuer", "cusip", "asset_class", "value", "principal_amount", "weight", "option_type"]) for r in records[:3]],
                "total_value": sum(r["value"] for r in records)}  # fmt: skip

    probe("form13f.sec.BRK-B", form13f, "BRK-B")
    probe("sec.institutions_search.vanguard", lambda: summary(obb.sec.institutions_search(query="vanguard", provider="sec"), ["name", "cik"], 5))  # fmt: skip

    # --- earnings ---------------------------------------------------------------------------------------------------
    start, end = (today() - timedelta(days=7)).isoformat(), today().isoformat()
    probe("earnings.nasdaq.calendar_recent", lambda: summary(obb.nasdaq.equity.calendar.earnings(start_date=start, end_date=end, provider="nasdaq"),
          ["report_date", "symbol", "name", "eps_actual", "eps_consensus", "eps_previous", "surprise_percent", "num_estimates", "period_ending", "reporting_time", "market_cap"], 4))  # fmt: skip
    probe("earnings.nasdaq.historical_eps.AAPL", lambda: summary(obb.nasdaq.equity.fundamental.historical_eps(symbol="AAPL", provider="nasdaq"),
          ["date", "eps_actual", "eps_estimated", "fiscal_period_ending", "surprise_percent"], 8))  # fmt: skip
    probe("earnings.nasdaq.historical_eps.RIVN", lambda: summary(obb.nasdaq.equity.fundamental.historical_eps(symbol="RIVN", provider="nasdaq"),
          ["date", "eps_actual", "eps_estimated", "fiscal_period_ending", "surprise_percent"], 6))  # fmt: skip

    def earnings_8k(symbol: str, cik: int) -> dict:
        recent = sec_json(f"https://data.sec.gov/submissions/CIK{cik10(cik)}.json")["filings"]["recent"]
        hits = [(d, a, i) for f, d, a, i in zip(recent["form"], recent["filingDate"], recent["accessionNumber"], recent["items"], strict=True)
                if f == "8-K" and "2.02" in i.split(",")]  # fmt: skip
        return {"symbol": symbol, "n_item_202": len(hits), "latest": hits[:4]}

    for symbol in ("AAPL", "MSFT", "NVDA"):
        probe(f"earnings.sec_8k_item_202.{symbol}", earnings_8k, symbol, ALL_CIKS[symbol])

    # --- news ---------------------------------------------------------------------------------------------------------
    def nasdaq_news(symbol: str) -> dict:
        res = obb.news.company(symbol=symbol, limit=40, provider="nasdaq")
        records = rows(res)
        dates = sorted(str(r["date"]) for r in records)
        return {"n": len(records), "oldest": dates[:1], "newest": dates[-1:], "publishers": dict(Counter(r.get("publisher") for r in records).most_common(8)),
                "topics": dict(Counter(r.get("topic") for r in records).most_common(5)), "has_body": sum(1 for r in records if r.get("body")),
                "has_excerpt": sum(1 for r in records if r.get("excerpt")), "sample": [pick(r, ["date", "title", "publisher", "url", "symbols"]) for r in records[:3]],
                "titles": [r["title"] for r in records]}  # fmt: skip

    news_cache: dict = {}

    def rss_titles(url: str) -> dict:
        response = http("GET", url, ua="Mozilla/5.0 (compatible; audit-probe)")
        response.raise_for_status()
        root = ET.fromstring(response.content)  # noqa: S314 - public RSS from a known host, read-only audit
        items = root.findall(".//item")
        parsed = [{"title": (i.findtext("title") or "").strip(), "pubDate": i.findtext("pubDate"), "link": i.findtext("link"),
                   "source": (i.findtext("source") or "").strip(), "has_description": bool((i.findtext("description") or "").strip())} for i in items]  # fmt: skip
        return {"n": len(parsed), "parsed": parsed}

    def rss_probe(name: str, url: str) -> dict:
        result = rss_titles(url)
        news_cache[name] = result
        return {"n": result["n"], "sample": result["parsed"][:3]}

    nasdaq = probe("news.nasdaq.AAPL", nasdaq_news, "AAPL")
    probe(
        "news.google_rss.AAPL",
        rss_probe,
        "google",
        "https://news.google.com/rss/search?q=AAPL+Apple+stock&hl=en-US&gl=US&ceid=US:en",
    )
    probe(
        "news.yahoo_rss.AAPL",
        rss_probe,
        "yahoo",
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL&region=US&lang=en-US",
    )

    def duplicates() -> dict:
        if not (nasdaq and "titles" in nasdaq):
            return {"note": "nasdaq news unavailable"}
        out = {}
        for name in ("google", "yahoo"):
            other = [i["title"] for i in news_cache.get(name, {}).get("parsed", [])]
            pairs = [
                (a, b)
                for a in nasdaq["titles"]
                for b in other
                if SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.85
            ]
            out[name] = {"n_other": len(other), "near_duplicate_pairs_over_0.85": len(pairs), "examples": pairs[:2]}
        return out

    probe("news.cross_source_duplicates", duplicates)
    probe("sec.rss_litigation", lambda: summary(obb.sec.rss_litigation(provider="sec"), ["published", "title", "link"], 3))  # fmt: skip
    probe("sec.full_text_search.8k_partnership", lambda: summary(obb.sec.full_text_search(query="strategic partnership agreement", forms="8-K", provider="sec"), ["filing_date", "company_name", "form", "url"], 4))  # fmt: skip

    # --- USAspending (not an OpenBB provider: direct, read-only search endpoints) --------------------------------
    api = "https://api.usaspending.gov/api/v2"

    def post(path: str, body: dict) -> dict:
        response = http("POST", api + path, json=body, headers={"Content-Type": "application/json"})
        return {"status": response.status_code, "json": response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text[:300]}  # fmt: skip

    probe(
        "usaspending.autocomplete_recipient.lockheed",
        lambda: post("/autocomplete/recipient/", {"search_text": "LOCKHEED MARTIN", "limit": 8}),
    )
    probe(
        "usaspending.autocomplete_recipient.microsoft",
        lambda: post("/autocomplete/recipient/", {"search_text": "MICROSOFT", "limit": 8}),
    )
    probe("usaspending.recipient_search.keyword", lambda: post("/recipient/", {"keyword": "LOCKHEED MARTIN", "limit": 5, "page": 1, "sort": "amount", "order": "desc"}))  # fmt: skip

    award_fields = ["Award ID", "Recipient Name", "Recipient UEI", "Start Date", "End Date", "Award Amount", "Awarding Agency", "Awarding Sub Agency",
                    "Contract Award Type", "Description", "Last Modified Date", "generated_internal_id"]  # fmt: skip
    first_award: dict = {}

    def awards() -> dict:
        res = post("/search/spending_by_award/", {
            "filters": {"award_type_codes": ["A", "B", "C", "D"], "recipient_search_text": ["LOCKHEED MARTIN"],
                        "time_period": [{"start_date": "2024-10-01", "end_date": "2025-09-30"}]},
            "fields": award_fields, "limit": 5, "page": 1, "sort": "Award Amount", "order": "desc"})  # fmt: skip
        results = res["json"].get("results", []) if isinstance(res["json"], dict) else []
        if results:
            first_award.update(results[0])
        return {"status": res["status"], "keys": sorted(res["json"]) if isinstance(res["json"], dict) else None, "n": len(results), "results": results[:3],
                "message": res["json"].get("detail") if isinstance(res["json"], dict) else res["json"]}  # fmt: skip

    probe("usaspending.awards.lockheed", awards)

    def award_detail() -> dict:
        gid = first_award.get("generated_internal_id") or first_award.get("internal_id")
        if not gid:
            return {"note": "no award from previous probe"}
        response = http("GET", f"{api}/awards/{gid}/")
        data = response.json()
        recipient = data.get("recipient") or {}
        return {"status": response.status_code, "keys": sorted(data)[:25], "recipient_keys": sorted(recipient), "recipient": {k: recipient.get(k) for k in ("recipient_name", "recipient_uei", "parent_recipient_name", "parent_recipient_uei", "business_categories")},
                "total_obligation": data.get("total_obligation"), "date_signed": data.get("date_signed"), "type": data.get("type_description"), "period": data.get("period_of_performance"),
                "transaction_count_hint": data.get("transaction_obligated_amount")}  # fmt: skip

    probe("usaspending.award_detail", award_detail)

    def transactions() -> dict:
        gid = first_award.get("generated_internal_id")
        if not gid:
            return {"note": "no award from previous probe"}
        res = post("/transactions/", {"award_id": gid, "limit": 5, "sort": "action_date", "order": "desc"})
        data = res["json"]
        return {"status": res["status"], "keys": sorted(data) if isinstance(data, dict) else None,
                "results": (data.get("results") or [])[:4] if isinstance(data, dict) else data}  # fmt: skip

    probe("usaspending.award_transactions(modifications)", transactions)

    # --- 8-K item taxonomy for event-type strategy -----------------------------------------------------------------
    def item_counts(symbol: str) -> dict:
        recent = sec_json(f"https://data.sec.gov/submissions/CIK{cik10(ALL_CIKS[symbol])}.json")["filings"]["recent"]
        cutoff = (today() - timedelta(days=730)).isoformat()
        counts = Counter(i for f, d, items in zip(recent["form"], recent["filingDate"], recent["items"], strict=True) if f == "8-K" and d >= cutoff for i in items.split(",") if i)  # fmt: skip
        return {"symbol": symbol, "items_2y": dict(counts.most_common(15))}

    for symbol in ("AAPL", "RIVN", "PTON"):
        probe(f"events.8k_items.{symbol}", item_counts, symbol)


# =====================================================================================================================
# group: followup  (open questions left after the first two probe rounds)
# =====================================================================================================================
def group_followup() -> None:
    obb = use_obb()

    # Q1 multi-class market cap: is it per class or whole-company, per provider?
    for symbol in ("GOOGL", "GOOG", "BRK.A", "BRK.B", "AAPL"):
        probe(f"followup.quote_nasdaq.{symbol}", lambda s=symbol: [
            pick(r, ["symbol", "last_price", "market_cap", "year_high", "year_low", "sector", "industry"])
            for r in rows(obb.nasdaq.equity.quote(symbol=s, provider="nasdaq"))])  # fmt: skip

    # Q2 company_type / provenance for the awkward filers
    def meta(symbol: str, statement: str = "income_statement", period: str = "annual") -> dict:
        res = getattr(obb.sec, statement)(symbol=symbol, period=period, limit=2, provider="sec")
        m = (getattr(res, "extra", {}) or {}).get("results_metadata") or {}
        return {"entity": m.get("entity_name"), "company_type": m.get("company_type"),
                "keys": sorted(m)[:12], "warnings": (m.get("validation_warnings") or [])[:4],
                "first_row_nonnull": sorted(k for k, v in rows(res)[0].items() if v is not None)[:60]}  # fmt: skip

    for symbol in ("BRK-B", "GOOGL", "NVDA", "RIVN", "JPM"):
        probe(f"followup.meta.{symbol}", meta, symbol)

    # Q3 net income attribution (RIVN-style non-controlling interests): OpenBB vs raw XBRL tags
    def net_income_tags(symbol: str, cik: int) -> dict:
        facts = sec_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10(cik)}.json")["facts"]["us-gaap"]
        out: dict = {}
        for tag in (
            "NetIncomeLoss",
            "ProfitLoss",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
            "IncomeLossFromContinuingOperations",
        ):
            if tag in facts:
                pts = [p for p in facts[tag]["units"]["USD"] if p.get("fp") == "FY"][-3:]
                out[tag] = [(p["end"], p["val"], p["form"], p["filed"]) for p in pts]
        res = rows(obb.sec.income_statement(symbol=symbol, period="annual", limit=3, provider="sec"))
        out["openbb"] = [pick(r, ["period_ending", "net_income", "consolidated_net_income", "net_income_continuing_operations", "net_income_attributable_to_noncontrolling_interest", "net_income_to_common"]) for r in res]  # fmt: skip
        return out

    for symbol in ("RIVN", "NVDA"):
        probe(f"followup.net_income.{symbol}", net_income_tags, symbol, ALL_CIKS[symbol])

    # Q4 NVDA split: default vs pit_mode, per-share and share fields across the 2024 10:1 split
    for pit in (False, True):
        probe(f"followup.nvda_split.pit_{pit}", lambda p=pit: summary(
            obb.sec.income_statement(symbol="NVDA", period="annual", limit=4, pit_mode=p, provider="sec"),
            ["period_ending", "fiscal_year", "total_revenue", "net_income", "diluted_eps", "weighted_average_diluted_shares_outstanding", "filing_date"], 4))  # fmt: skip

    # Q5 quarterly cash flow: discrete quarters or fiscal-year-to-date?
    probe("followup.cashflow.AAPL.quarterly", lambda: summary(
        obb.sec.cash_flow(symbol="AAPL", period="quarterly", limit=6, provider="sec"),
        ["period_ending", "fiscal_period", "fiscal_year", "net_cash_from_operating_activities", "capital_expenditures", "purchase_of_property_plant_and_equipment", "filing_date"], 6))  # fmt: skip

    # Q6 dei shares outstanding: single class vs multi class, as raw facts (filed date + accession)
    def dei_shares(symbol: str, cik: int) -> dict:
        facts = sec_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10(cik)}.json")["facts"].get("dei", {})
        out: dict = {"dei_tags": sorted(facts)}
        for tag in ("EntityCommonStockSharesOutstanding", "EntityPublicFloat"):
            if tag in facts:
                unit = next(iter(facts[tag]["units"]))
                pts = facts[tag]["units"][unit]
                out[tag] = {"unit": unit, "n": len(pts), "keys": sorted(pts[-1]),
                            "latest": [(p["end"], p["val"], p["form"], p["filed"], p.get("fy"), p.get("fp")) for p in pts[-4:]]}  # fmt: skip
        return out

    for symbol in ("AAPL", "GOOGL", "BRK-B"):
        probe(f"followup.dei_shares.{symbol}", dei_shares, symbol, ALL_CIKS[symbol])

    # Q7 single-object commands (previous round failed in my probe code, not at the provider)
    for symbol in ("AAPL", "BRK-B"):
        probe(f"followup.cik_map.{symbol}", lambda s=symbol: rows(obb.sec.cik_map(symbol=s, provider="sec")))
    probe("followup.symbol_map.320193", lambda: rows(obb.sec.symbol_map(query="320193", provider="sec")))
    probe("followup.symbol_map.1067983", lambda: rows(obb.sec.symbol_map(query="1067983", provider="sec")))


# =====================================================================================================================
# group: taxonomy  (Nasdaq sector/industry vs SEC SIC for representative issuers; screener industry vocabulary)
# =====================================================================================================================
def group_taxonomy() -> None:
    obb = use_obb()
    symbols = [
        "AAPL",
        "MSFT",
        "NVDA",
        "AMD",
        "AVGO",
        "META",
        "AMZN",
        "GOOGL",
        "INTC",
        "QCOM",
        "TSM",
        "ASML",
        "MU",
        "TXN",
    ]
    for symbol in symbols:
        probe(f"taxonomy.nasdaq_quote.{symbol}", lambda s=symbol: [
            pick(r, ["symbol", "name", "exchange", "sector", "industry"])
            for r in rows(obb.nasdaq.equity.quote(symbol=s, provider="nasdaq"))])  # fmt: skip
        probe(f"taxonomy.nasdaq_profile.{symbol}", lambda s=symbol: [
            pick(r, ["symbol", "sector", "industry_category", "industry_group", "stock_type", "exchange", "sic", "cik"])
            for r in rows(obb.nasdaq.equity.profile(symbol=s, provider="nasdaq"))])  # fmt: skip

    def sic(symbol: str) -> dict:
        cik = rows(obb.sec.cik_map(symbol=symbol, provider="sec"))[0]["cik"]
        data = sec_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
        return {"symbol": symbol, "cik": cik, "sic": data.get("sic"), "sicDescription": data.get("sicDescription")}

    for symbol in symbols:
        probe(f"taxonomy.sec_sic.{symbol}", sic, symbol)

    def screener(sector: str, exchange: str) -> dict:
        records = rows(obb.nasdaq.equity.screener(sector=sector, exchange=exchange, limit=10000, provider="nasdaq"))
        industries = Counter(r.get("industry") for r in records)
        found = {r["symbol"]: [r.get("sector"), r.get("industry")] for r in records if r["symbol"] in symbols}
        return {"n": len(records), "columns": sorted(records[0]) if records else [], "n_industries": len(industries),
                "industries": industries.most_common(80), "representatives": found,
                "exchanges_sample": Counter(r.get("exchange") for r in records).most_common(6)}  # fmt: skip

    probe("taxonomy.screener.technology.all", screener, "technology", "all")
    for sector in ("consumer_discretionary", "communication_services"):
        probe(f"taxonomy.screener.{sector}.all", screener, sector, "all")


GROUPS = {"connect_identity": group_connect_identity, "market": group_market, "fundamentals": group_fundamentals,
          "ownership_events": group_ownership_events, "followup": group_followup, "taxonomy": group_taxonomy}  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=sorted(GROUPS), required=True)
    parser.add_argument("--out", type=Path, default=None, help="also write all results as JSON to this file")
    parser.add_argument("--only", default="", help="regex: run only probes whose id matches (full detail is printed)")
    parser.add_argument("--digest", action="store_true", help="print a one-line digest per probe at the end")
    args = parser.parse_args()
    if args.only:
        _only.append(re.compile(args.only))
    started = time.monotonic()
    GROUPS[args.group]()
    if args.digest:
        for r in _results:
            gist = json.dumps(r["info"], default=str)[:150]
            print(f"DIGEST {'OK  ' if r['ok'] else 'FAIL'} {r['id']} {r['ms']}ms {gist}")
    ok = sum(1 for r in _results if r["ok"])
    print(
        f"PROBE_SUMMARY group={args.group} total={len(_results)} ok={ok} failed={len(_results) - ok} seconds={time.monotonic() - started:.0f}"
    )
    if args.out:
        args.out.write_text(json.dumps(_results, default=str), encoding="utf-8")
    return 0  # an audit records failures; it does not fail the build


if __name__ == "__main__":
    raise SystemExit(main())
