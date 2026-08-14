"""Rate limiting and retry for public governance APIs.

Both source families push back under load and neither is ours to strain: Snapshot serves a
shared public endpoint, and every Discourse forum is an independent instance run by the
protocol's own community. Throttling is built in from the start rather than bolted on after
the first 429.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable

import requests

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RateLimitError(RuntimeError):
    """Raised when a request is still being throttled after the last attempt."""


class TokenBucket:
    """Classic token bucket. Thread-safe, and injectable clock/sleep so the timing
    behaviour can be unit-tested without real waits."""

    def __init__(
        self,
        rate_per_minute: float,
        capacity: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self.rate_per_second = rate_per_minute / 60.0
        self.capacity = capacity if capacity is not None else max(1, int(rate_per_minute // 6))
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(self.capacity)
        self._updated = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_second)
            self._updated = now

    def acquire(self, tokens: float = 1.0) -> None:
        """Block until `tokens` are available."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                deficit = tokens - self._tokens
                wait = deficit / self.rate_per_second
            self._sleep(wait)


class HttpClient:
    """Rate-limited HTTP with retries on throttling, server errors and transport faults.

    Client errors other than 429 are *not* retried — a 404 is a bug in the request, and
    retrying it only burns rate budget that a real request needs.
    """

    def __init__(
        self,
        bucket: TokenBucket,
        session: requests.Session | None = None,
        max_attempts: int = 5,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        user_agent: str = "crypto-gov-rag/0.1 (governance research; contact via repo)",
    ):
        self.bucket = bucket
        self.session = session if session is not None else requests.Session()
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.timeout = timeout
        self._sleep = sleep
        self.user_agent = user_agent

    def _retry_delay(self, attempt: int, response) -> float:
        """Honour Retry-After when the server sends it; otherwise exponential backoff
        with jitter so parallel workers do not resynchronise into a thundering herd."""
        if response is not None:
            raw = response.headers.get("Retry-After")
            if raw:
                try:
                    return min(float(raw), self.backoff_cap)
                except ValueError:
                    pass
        delay = min(self.backoff_base * (2**attempt), self.backoff_cap)
        return delay + random.uniform(0, delay * 0.1)

    def request_json(self, method: str, url: str, **kwargs) -> dict:
        headers = {"User-Agent": self.user_agent, **kwargs.pop("headers", {})}
        kwargs.setdefault("timeout", self.timeout)
        last_response = None

        for attempt in range(self.max_attempts):
            self.bucket.acquire()
            try:
                response = self.session.request(method, url, headers=headers, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt == self.max_attempts - 1:
                    raise
                delay = self._retry_delay(attempt, None)
                log.warning(
                    "%s %s transport error (%s); retrying in %.1fs", method, url, exc, delay
                )
                self._sleep(delay)
                continue

            if response.status_code in RETRYABLE_STATUS:
                last_response = response
                if attempt == self.max_attempts - 1:
                    break
                delay = self._retry_delay(attempt, response)
                log.warning(
                    "%s %s returned %s; retrying in %.1fs (attempt %d/%d)",
                    method,
                    url,
                    response.status_code,
                    delay,
                    attempt + 1,
                    self.max_attempts,
                )
                self._sleep(delay)
                continue

            response.raise_for_status()
            return response.json()

        status = last_response.status_code if last_response is not None else "unknown"
        raise RateLimitError(
            f"{method} {url} still failing with {status} after {self.max_attempts} attempts"
        )
