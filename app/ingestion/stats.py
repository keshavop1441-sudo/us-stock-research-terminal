"""Request accounting shared by every fetcher, so the report can state request count, rate, 403s and 429s."""

import threading
import time
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class RequestStats:
    """Thread-safe counters. ``status`` None (no HTTP status known: OpenBB call, transport error) is stored as
    'error'/'ok'."""

    provider_requests: Counter = field(default_factory=Counter)
    status_counts: Counter = field(default_factory=Counter)  # (provider, status)
    retries: int = 0
    bytes_received: int = 0
    first_at: float | None = None
    _times: dict = field(default_factory=dict, repr=False)
    last_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, provider: str, status: int | str, *, size: int = 0, retry: bool = False) -> None:
        with self._lock:
            now = time.monotonic()
            self.first_at = now if self.first_at is None else self.first_at
            self.last_at = now
            self.provider_requests[provider] += 1
            self._times.setdefault(provider, []).append(now)
            self.status_counts[(provider, str(status))] += 1
            self.bytes_received += size
            self.retries += int(retry)

    def total(self, provider: str | None = None) -> int:
        return sum(self.provider_requests.values()) if provider is None else self.provider_requests[provider]

    def count_status(self, status: int, provider: str | None = None) -> int:
        return sum(n for (p, s), n in self.status_counts.items() if s == str(status) and provider in (None, p))

    def requests_per_second(self, provider: str | None = None) -> float | None:
        if self.first_at is None or self.last_at is None or self.last_at <= self.first_at:
            return None
        return self.total(provider) / (self.last_at - self.first_at)

    def max_requests_in_any_second(self, provider: str) -> int:
        """Most requests of ``provider`` that fell inside one sliding 1-second window (the sustained-rate check)."""
        times = self._times.get(provider, [])
        best, left = 0, 0
        for right, t in enumerate(times):
            while t - times[left] >= 1.0:
                left += 1
            best = max(best, right - left + 1)
        return best

    def snapshot(self) -> dict[str, object]:
        return {
            "requests_total": self.total(),
            "requests_by_provider": dict(self.provider_requests),
            "http_403": self.count_status(403),
            "http_429": self.count_status(429),
            "status_counts": {f"{p}:{s}": n for (p, s), n in sorted(self.status_counts.items())},
            "retries": self.retries,
            "bytes_received": self.bytes_received,
            "requests_per_second_overall": self.requests_per_second(),
            "max_requests_in_any_second": {p: self.max_requests_in_any_second(p) for p in self.provider_requests},
        }
