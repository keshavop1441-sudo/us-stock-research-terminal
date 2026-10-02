"""SEC HTTP access: identifying User-Agent, a request-rate ceiling, bounded retries with backoff, typed failures.

SEC fair-access policy (docs/data_coverage.md section 5): at most 10 requests/second and a User-Agent that identifies
the caller. The ceiling here is 1 request per ``min_interval`` seconds (default 0.12 s = at most ~8.3/s, under the
audit's 9/s acceptance limit). Retries are for transient conditions only:

  * 429, 5xx, transport errors: retried up to ``max_attempts`` with exponential backoff (``Retry-After`` honoured,
    capped).
  * 403: NEVER retried. It means the User-Agent/contact is not accepted or the caller is blocked; once seen, every
    later call fails fast WITHOUT touching the network (hammering a block makes it worse).
  * 404: not retried (the resource does not exist).
  * non-JSON / non-object body: ``MalformedResponseError``, not retried.

The User-Agent comes from ``Settings.sec_user_agent`` (env ``SEC_USER_AGENT``). If it is missing the client refuses to
exist (``MissingUserAgentError``): no placeholder or invented contact is ever substituted.
"""

import json
import time
from collections.abc import Callable
from contextlib import suppress
from urllib.parse import urlsplit

import httpx

from app.ingestion.errors import (
    AccessDeniedError,
    MalformedResponseError,
    MissingUserAgentError,
    NetworkError,
    NotFoundError,
    RateLimitedError,
    ServerError,
)
from app.ingestion.retrieval import Retrieval, utc_now
from app.ingestion.stats import RequestStats

ALLOWED_HOSTS = frozenset({"www.sec.gov", "data.sec.gov"})
MAX_RETRY_AFTER_SECONDS = 60.0
_TRANSPORT_ERRORS = (httpx.TransportError,)


class SecHttpClient:
    def __init__(
        self,
        user_agent: str | None,
        stats: RequestStats,
        *,
        transport: httpx.BaseTransport | None = None,
        min_interval: float = 0.12,
        max_attempts: int = 4,
        backoff_base: float = 1.0,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not user_agent or not user_agent.strip():
            raise MissingUserAgentError("SEC_USER_AGENT is not set: live SEC ingestion cannot proceed.")
        self._stats = stats
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
        )
        self._min_interval = min_interval
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._sleep, self._clock = sleep, clock
        self._last_request: float | None = None
        self.blocked = False  # set by the first 403

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SecHttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _pace(self) -> None:
        if self._last_request is not None:
            wait = self._min_interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def get_json(self, url: str, dataset: str, parameters: dict[str, object] | None = None) -> Retrieval:
        host = urlsplit(url).hostname
        if host not in ALLOWED_HOSTS:
            raise ValueError(f"refusing to request a non-SEC host: {host!r}")
        if self.blocked:
            raise AccessDeniedError("SEC previously answered HTTP 403 in this run; not sending further requests.")
        last_error: Exception | None = None
        requests_made = 0
        for attempt in range(1, self._max_attempts + 1):
            self._pace()
            requests_made += 1
            try:
                response = self._client.get(url)
            except _TRANSPORT_ERRORS as exc:
                self._stats.record("sec", "error", retry=attempt > 1)
                last_error = NetworkError(f"{type(exc).__name__}: {exc}")
                self._backoff(attempt, None)
                continue
            status = response.status_code
            self._stats.record("sec", status, size=len(response.content), retry=attempt > 1)
            if status == 403:
                self.blocked = True
                raise AccessDeniedError(
                    f"HTTP 403 from {url}: the User-Agent contact was not accepted or the caller is blocked."
                )
            if status == 404:
                raise NotFoundError(f"HTTP 404 from {url}")
            if status == 429 or status >= 500:
                last_error = (
                    RateLimitedError(f"HTTP 429 from {url} after {attempt} attempt(s)")
                    if status == 429
                    else ServerError(f"HTTP {status} from {url} after {attempt} attempt(s)")
                )
                self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            if status != 200:
                raise ServerError(f"unexpected HTTP {status} from {url}")
            return self._parse(response, url, dataset, parameters or {}, requests_made)
        assert last_error is not None
        raise last_error

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if attempt >= self._max_attempts:
            return  # no sleep after the last attempt
        delay = self._backoff_base * 2 ** (attempt - 1)
        if retry_after:
            with suppress(ValueError):  # an HTTP-date Retry-After: keep the computed backoff
                delay = max(delay, min(float(retry_after), MAX_RETRY_AFTER_SECONDS))
        self._sleep(delay)

    @staticmethod
    def _parse(response: httpx.Response, url: str, dataset: str, parameters: dict, requests_made: int) -> Retrieval:
        try:
            payload = json.loads(response.content)
        except ValueError as exc:
            raise MalformedResponseError(f"{url}: body is not JSON ({exc})") from exc
        if not isinstance(payload, dict):
            raise MalformedResponseError(f"{url}: expected a JSON object, got {type(payload).__name__}")
        return Retrieval(
            provider="sec",
            dataset=dataset,
            command=f"HTTP GET {url}",
            parameters=parameters,
            payload=payload,
            retrieved_at=utc_now(),
            url=url,
            request_count=requests_made,
        )
