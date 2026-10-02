"""Market data through OpenBB V5 (``obb.cboe.equity.historical`` primary, ``obb.nasdaq.equity.historical`` fallback,
``obb.nasdaq.equity.quote`` for the quote, market cap and Nasdaq's raw sector/industry).

Provider spellings differ (SEC ``BRK-B``; Nasdaq and Cboe ``BRK.B``): the conversion is
``app.models.symbols.provider_symbol``. ``obb`` is injected, so tests use a fake and ``import openbb`` stays lazy
(``app.data.openbb_client.get_obb``).

OpenBB hides the HTTP layer: one OpenBB call is counted as one request (a lower bound; an extension may issue
several). 403/429 statuses are therefore invisible here and only the SEC client can count them; OpenBB failures are
counted as ``error``.
"""

from collections.abc import Callable
from datetime import date, datetime
from importlib.metadata import PackageNotFoundError, version

from app.ingestion.errors import EmptyDataError, IngestionError, NetworkError
from app.ingestion.retrieval import Retrieval, utc_now
from app.ingestion.stats import RequestStats
from app.models.symbols import provider_symbol

PRICE_PRIMARY = "cboe"
PRICE_FALLBACK = "nasdaq"
_PACKAGE = {"cboe": "openbb-cboe", "nasdaq": "openbb-nasdaq"}


def provider_version(provider: str) -> str | None:
    package = _PACKAGE[provider]
    try:
        return f"{package} {version(package)}"
    except PackageNotFoundError:
        return None


def _rows(result: object) -> list[dict]:
    """OBBject.results (a list of pydantic models) -> plain dicts."""
    items = getattr(result, "results", None)
    if not items:
        raise EmptyDataError("provider returned no rows")
    return [item.model_dump() if hasattr(item, "model_dump") else dict(item) for item in items]


class MarketData:
    def __init__(self, obb_getter: Callable[[], object], stats: RequestStats):
        self._get_obb = obb_getter
        self._stats = stats

    def _call(self, provider: str, namespace: str, command: str, symbol: str, **kwargs: object) -> Retrieval:
        sent = provider_symbol(symbol, provider)
        target = self._get_obb()
        for part in (provider, *namespace.split(".")):
            target = getattr(target, part)
        full = f"obb.{provider}.{namespace}.{command}"
        params = {"symbol": sent, "provider": provider, **{k: str(v) for k, v in kwargs.items()}}
        try:
            rows = _rows(getattr(target, command)(symbol=sent, provider=provider, **kwargs))  # explicit, as probed
        except IngestionError:
            self._stats.record(provider, "error")
            raise
        except Exception as exc:  # noqa: BLE001 - OpenBB raises many provider-specific types (OpenBBError, EmptyDataError, ...)
            self._stats.record(provider, "error")
            raise NetworkError(f"{full}({sent}): {type(exc).__name__}: {exc}") from exc
        self._stats.record(provider, 200)
        return Retrieval(
            provider=provider,
            dataset=f"{namespace}.{command}",
            command=full,
            parameters=params,
            payload=rows,
            retrieved_at=utc_now(),
            provider_version=provider_version(provider),
        )

    def historical(self, symbol: str, start: date, end: date) -> Retrieval:
        """Daily bars from the primary provider, else the fallback (marked ``is_fallback``). Raises if both fail."""
        kwargs = {"start_date": start.isoformat(), "end_date": end.isoformat()}
        try:
            return self._call(PRICE_PRIMARY, "equity", "historical", symbol, **kwargs)
        except IngestionError as primary_error:
            try:
                fallback = self._call(PRICE_FALLBACK, "equity", "historical", symbol, **kwargs)
            except IngestionError as fallback_error:
                raise NetworkError(
                    f"both price providers failed for {symbol}: {PRICE_PRIMARY}: {primary_error}; "
                    f"{PRICE_FALLBACK}: {fallback_error}"
                ) from fallback_error
            fallback.is_fallback = True
            fallback.notes.append(f"primary {PRICE_PRIMARY} failed: {primary_error}")
            return fallback

    def quote(self, symbol: str) -> Retrieval:
        """Nasdaq quote: last price, quoted market cap (price x ALL issuer shares), year high/low, raw
        sector/industry."""
        retrieval = self._call("nasdaq", "equity", "quote", symbol)
        stamp = retrieval.payload[0].get("last_timestamp")  # type: ignore[index]
        if isinstance(stamp, datetime):
            retrieval.as_of = stamp.replace(tzinfo=None)
        elif isinstance(stamp, date):
            retrieval.as_of = datetime(stamp.year, stamp.month, stamp.day)
        return retrieval
