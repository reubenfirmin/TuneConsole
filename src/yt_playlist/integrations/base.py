"""Vendor access outcomes and shared request pacing."""
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Availability:
    retry_at: float = 0
    error: str = ''

    def ready(self, now):
        return self.retry_at <= now


class IntegrationUnavailable(Exception):
    """A retryable access failure, including the actual upstream dependency."""

    def __init__(self, provider, message, retry_at, *, outage=True):
        super().__init__(message)
        self.provider = provider
        self.retry_at = retry_at
        self.outage = outage


class RateLimiter:
    """Space request slots across threads; each vendor owns its interval."""

    def __init__(self, min_interval):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self, min_interval=None):
        interval = self.min_interval if min_interval is None else min_interval
        with self._lock:
            delay = interval - (time.monotonic() - self._last_call)
            if delay > 0:
                time.sleep(delay)
            self._last_call = time.monotonic()
