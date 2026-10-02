"""SEC HTTP client: User-Agent, rate ceiling, bounded retries/backoff, 403/429/5xx/network/garbage handling (no
network)."""

import httpx
import pytest

from app.ingestion.errors import (
    AccessDeniedError,
    MalformedResponseError,
    MissingUserAgentError,
    NetworkError,
    NotFoundError,
    RateLimitedError,
    ServerError,
)
from app.ingestion.sec_http import SecHttpClient
from app.ingestion.stats import RequestStats

URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
UA = "UnitTestApp contact@example.org"


def client(handler, **kw):
    sleeps: list[float] = []
    stats = RequestStats()
    c = SecHttpClient(UA, stats, transport=httpx.MockTransport(handler), sleep=sleeps.append, min_interval=0, **kw)
    return c, stats, sleeps


def script(*responses):
    """A handler that replays status codes (or exceptions) in order, then 200 {}."""
    queue = list(responses)
    calls = []

    def handler(request):
        calls.append(request)
        item = queue.pop(0) if queue else 200
        if isinstance(item, Exception):
            raise item
        if item == 200:
            return httpx.Response(200, json={"ok": True})
        headers = {"Retry-After": "7"} if item == 429 else {}
        return httpx.Response(item, headers=headers)

    handler.calls = calls
    return handler


def test_no_user_agent_means_no_client_and_nothing_is_invented():
    for value in (None, "", "   "):
        with pytest.raises(MissingUserAgentError):
            SecHttpClient(value, RequestStats())


def test_the_configured_user_agent_is_sent_and_json_is_returned_with_provenance():
    handler = script(200)
    c, stats, _ = client(handler)
    got = c.get_json(URL, "companyfacts", {"cik": "0000320193"})
    assert handler.calls[0].headers["user-agent"] == UA
    assert got.payload == {"ok": True} and got.provider == "sec" and got.url == URL
    assert (
        got.command == f"HTTP GET {URL}" and got.parameters == {"cik": "0000320193"} and got.hash.startswith("sha256:")
    )
    assert got.retrieved_at.tzinfo is None
    assert stats.total("sec") == 1 and stats.count_status(200) == 1


def test_a_429_is_retried_with_backoff_honouring_retry_after_then_succeeds():
    handler = script(429, 429, 200)
    c, stats, sleeps = client(handler)
    assert c.get_json(URL, "companyfacts").payload == {"ok": True}
    assert len(handler.calls) == 3 and stats.count_status(429) == 2 and stats.retries == 2
    assert sleeps == [7.0, 7.0]  # Retry-After (7) beats the 1s/2s exponential backoff


def test_a_persistent_429_stops_after_the_bounded_number_of_attempts():
    handler = script(*[429] * 10)
    c, stats, sleeps = client(handler, max_attempts=3)
    with pytest.raises(RateLimitedError):
        c.get_json(URL, "companyfacts")
    assert len(handler.calls) == 3 and stats.count_status(429) == 3
    assert len(sleeps) == 2  # no pointless sleep after the last attempt


def test_5xx_and_transport_errors_are_retried_then_reported_as_their_own_class():
    c, _, _ = client(script(503, 502, 500, 500), max_attempts=3)
    with pytest.raises(ServerError):
        c.get_json(URL, "x")
    c, stats, _ = client(
        script(httpx.ConnectError("boom"), httpx.ReadTimeout("slow"), httpx.ConnectError("again")), max_attempts=3
    )
    with pytest.raises(NetworkError):
        c.get_json(URL, "x")
    assert stats.status_counts[("sec", "error")] == 3
    c, _, _ = client(script(httpx.ConnectError("blip"), 200))
    assert c.get_json(URL, "x").payload == {"ok": True}  # a transient blip recovers


def test_403_is_never_retried_and_blocks_every_later_request_without_touching_the_network():
    handler = script(403)
    c, stats, _ = client(handler)
    with pytest.raises(AccessDeniedError, match="403"):
        c.get_json(URL, "x")
    with pytest.raises(AccessDeniedError, match="not sending further requests"):
        c.get_json(URL.replace("0000320193", "0000789019"), "x")
    assert len(handler.calls) == 1 and stats.count_status(403) == 1


def test_404_is_not_retried():
    handler = script(404)
    c, _, _ = client(handler)
    with pytest.raises(NotFoundError):
        c.get_json(URL, "x")
    assert len(handler.calls) == 1


@pytest.mark.parametrize("body", [b"<html>nope</html>", b"[1, 2]", b""])
def test_malformed_bodies_are_rejected_not_parsed(body):
    c, _, _ = client(lambda request: httpx.Response(200, content=body))
    with pytest.raises(MalformedResponseError):
        c.get_json(URL, "x")


def test_only_sec_hosts_are_ever_requested():
    handler = script(200)
    c, _, _ = client(handler)
    with pytest.raises(ValueError, match="non-SEC host"):
        c.get_json("https://example.com/x.json", "x")
    assert not handler.calls


def test_requests_are_paced_to_the_minimum_interval():
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    stats = RequestStats()
    c = SecHttpClient(
        UA, stats, transport=httpx.MockTransport(script()), min_interval=0.12, sleep=sleep, clock=lambda: now[0]
    )
    for _ in range(4):
        c.get_json(URL, "x")
    assert sleeps == pytest.approx([0.12, 0.12, 0.12])  # 8.3 requests/second ceiling, below the audit's 9/s limit
