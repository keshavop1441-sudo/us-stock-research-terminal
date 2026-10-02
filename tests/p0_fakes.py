"""SIMULATED providers for hermetic P0 tests and the offline rehearsal. Every value is invented (see p0_synthetic).

* ``FakeSec``       an ``httpx.MockTransport`` handler serving SEC-shaped documents, with switches for
  403/429/5xx/garbage.
* ``FakeObb``       the slice of the OpenBB V5 object the pipeline uses (``obb.<provider>.equity.<command>``).
"""

import json
import random
import warnings
from datetime import date, timedelta

import httpx
import p0_synthetic as syn

from app.models.symbols import canonical_symbol

# single-class issuers get the same count as the synthetic dei cover-page value, so the A12 market-cap check is exact
DEFAULT_SHARES = 14.6e9
SHARES = {"AAPL": 14.6e9, "GOOGL": 12.1e9, "GOOG": 12.1e9, "BRK-B": 2.2e9}


class FakeSec:
    def __init__(self, symbols=tuple(syn.CIKS)):
        self.symbols = tuple(symbols)
        self.requests: list[str] = []
        self.user_agents: set[str] = set()
        self.forced: dict[
            str, list[int | str]
        ] = {}  # url fragment -> responses to give first (status code or 'garbage')
        self.docs_overrides: dict[str, dict] = {}  # url fragment -> replacement document
        self.map_override: dict | None = None
        self.forms: dict[str, tuple] = {}  # symbol -> submissions ``forms`` tuples (form, filed, report[, items])

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def facts_doc(self, symbol: str) -> dict:
        cik = syn.CIKS[symbol]
        if symbol == "TSM":  # foreign private issuer: IFRS, nothing under us-gaap
            ifrs_point = {"accn": "0000000000-26-000001", "fy": 2025, "fp": "FY", "form": "20-F", "filed": "2026-03-01"}
            return {  # the live taxonomies were ['dei', 'ifrs-full', 'srt']; every number here is invented
                "cik": cik,
                "entityName": "SYNTHETIC TSM",
                "facts": {
                    "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [
                        {**ifrs_point, "end": "2026-02-28", "val": 5.2e9}]}}},
                    "ifrs-full": {"Revenue": {"units": {"TWD": [
                        {**ifrs_point, "start": "2025-01-01", "end": "2025-12-31", "val": 1.0e12}]}}},
                    "srt": {},
                },
            }  # fmt: skip
        if symbol == "JPM":  # bank: no gross profit / operating income / capex lines
            return syn.standard_issuer(cik, gross_profit=False, capex=False, debt_lines=())
        if symbol == "BRK-B":  # no debt lines and no EPS
            doc = syn.standard_issuer(cik, debt_lines=(), dei_shares=0)
            doc["facts"]["us-gaap"].pop("EarningsPerShareDiluted")
            return doc
        if symbol in ("RIVN", "PTON"):
            return syn.standard_issuer(cik, net_margin=-0.4, eps_base=-3.0)
        if symbol == "NVDA":
            return syn.standard_issuer(cik, split_in_q1=10)
        if symbol in ("GOOGL", "GOOG"):
            return syn.standard_issuer(cik, dei_shares=0)
        return syn.standard_issuer(cik)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        self.user_agents.add(request.headers.get("user-agent", ""))
        for fragment, queue in self.forced.items():
            if fragment in url and queue:
                code = queue.pop(0)
                if code == "garbage":
                    return httpx.Response(200, content=b"<html>not json</html>")
                return httpx.Response(int(code), headers={"Retry-After": "0"} if code == 429 else {}, content=b"")
        for fragment, doc in self.docs_overrides.items():
            if fragment in url:
                return httpx.Response(200, content=syn.dumps(doc))
        if url.endswith("company_tickers_exchange.json"):
            return httpx.Response(200, content=syn.dumps(self.map_override or syn.ticker_map(self.symbols)))
        by_cik = {syn.CIKS[s]: s for s in self.symbols}
        for cik, symbol in by_cik.items():
            if url.endswith(f"submissions/CIK{cik:010d}.json"):
                doc = syn.submissions(cik, symbol, **({"forms": self.forms[symbol]} if symbol in self.forms else {}))
                doc["tickers"] = [s for s in self.symbols if syn.CIKS[s] == cik]  # every listing of the issuer
                return httpx.Response(200, content=syn.dumps(doc))
            if url.endswith(f"companyfacts/CIK{cik:010d}.json"):
                return httpx.Response(200, content=syn.dumps(self.facts_doc(symbol)))
        return httpx.Response(404, content=b"{}")


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self):
        return dict(self.__dict__)


class _Result:
    def __init__(self, rows):
        self.results = rows


def business_days(start: date, end: date):
    day = start
    while day <= end:
        if day.weekday() < 5:
            yield day
        day += timedelta(days=1)


class _Namespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeObb:
    """``obb.cboe.equity.historical`` / ``obb.nasdaq.equity.historical`` / ``obb.nasdaq.equity.quote`` with SIMULATED
    rows."""

    def __init__(self, as_of: date):
        self.as_of = as_of
        self.calls: list[tuple[str, str, str]] = []
        self.fail: dict[tuple[str, str], Exception] = {}  # (provider, spelled symbol) -> error to raise
        self.duplicate_rows: set[str] = set()
        self.bad_rows: set[str] = set()
        self.drop_quote_for: set[str] = set()
        self.conflicting_duplicates: set[str] = set()
        self.no_classification: set[str] = set()
        self.warn_on: dict[tuple[str, str], str] = {}  # (provider, command) -> Python warning text to emit
        self.cap_scale: dict[str, float] = {}  # symbol -> factor applied to the quoted market cap
        self.cboe = _Namespace(equity=_Namespace(historical=self._historical("cboe")))
        self.nasdaq = _Namespace(equity=_Namespace(historical=self._historical("nasdaq"), quote=self._quote))

    @staticmethod
    def _symbol(spelled: str) -> str:
        return canonical_symbol(spelled)

    def _bars(self, symbol: str, start, end):
        rng = random.Random(symbol)
        price = 20 + rng.random() * 200
        rows = []
        # one fixed series from 2020 per symbol, sliced by date, so history and quote agree whatever window is asked for
        for day in business_days(date(2020, 1, 2), max(end, self.as_of)):
            price *= 1 + rng.uniform(-0.02, 0.0215)
            high, low = price * 1.01, price * 0.99
            if start <= day <= end:
                rows.append({"date": day.isoformat(), "open": round(price, 2), "high": round(high, 2),
                             "low": round(low, 2), "close": round(price, 2),
                             "volume": int(1e6 * (1 + rng.random()))})  # fmt: skip
            else:
                rng.random()  # keep the random stream aligned with the volume draw
        return rows

    def _historical(self, provider: str):
        def call(symbol, start_date=None, end_date=None, **_):
            self.calls.append((provider, "historical", symbol))
            if provider == "cboe" and "-" in symbol:  # live: Cboe rejects the SEC spelling BRK-B (HTTP 403)
                raise RuntimeError("HTTP 403 for symbol spelled with '-'")
            if (provider, symbol) in self.fail:
                raise self.fail[(provider, symbol)]
            canonical = self._symbol(symbol)
            if (provider, "historical") in self.warn_on:
                warnings.warn(self.warn_on[(provider, "historical")], UserWarning, stacklevel=2)
            start = date.fromisoformat(start_date) if start_date else date(2020, 1, 2)
            end = date.fromisoformat(end_date) if end_date else self.as_of
            rows = self._bars(canonical, start, end)
            if canonical in self.duplicate_rows:
                rows = rows + rows[-3:]
            if canonical in self.conflicting_duplicates:
                rows = rows + [{**rows[-1], "close": rows[-1]["close"], "volume": rows[-1]["volume"] + 1}]
            if canonical in self.bad_rows:
                rows = rows + [{"date": "2025-01-02", "open": 5, "high": 4, "low": 6, "close": 5, "volume": 1}]
            return _Result([_Row(**r) for r in rows])

        return call

    def _quote(self, symbol, **_):
        self.calls.append(("nasdaq", "quote", symbol))
        canonical = self._symbol(symbol)
        if canonical in self.drop_quote_for or ("nasdaq", symbol) in self.fail:
            raise self.fail.get(("nasdaq", symbol), RuntimeError("quote unavailable"))
        bars = self._bars(canonical, self.as_of - timedelta(days=400), self.as_of)
        last = bars[-1]["close"]
        year = [b for b in bars if b["date"] >= (self.as_of - timedelta(days=365)).isoformat()]
        shares = SHARES.get(canonical, DEFAULT_SHARES)
        return _Result([
            _Row(symbol=symbol, last_price=last, market_cap=last * shares * self.cap_scale.get(canonical, 1.0),
                 year_high=max(b["high"] for b in year), year_low=min(b["low"] for b in year),
                 last_timestamp=date.fromisoformat(bars[-1]["date"]),
                 sector=None if canonical in self.no_classification else "Technology",
                 industry=None if canonical in self.no_classification else "Computer Manufacturing",
                 exchange="NASDAQ-GS")
        ])  # fmt: skip


def json_bytes(obj) -> bytes:
    return json.dumps(obj).encode()
