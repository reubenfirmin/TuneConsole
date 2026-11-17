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


class AccessState:
    """Narrow infrastructure access-state port for an IAO's provider cooldown.

    An IAO's own pacing/availability/cache are legitimate IAO concerns; the
    durable cooldown it reads/writes across requests and processes is not --
    it belongs to the application's enrichment-state controller. This exposes
    exactly those three operations, so an IAO never holds a whole Store or
    repo-shaped service locator, only this.
    """

    __slots__ = ('_enrichment_state',)

    def __init__(self, enrichment_state):
        self._enrichment_state = enrichment_state

    @classmethod
    def for_store(cls, store):
        # A bounded fake lacking enrichment_state (e.g. in core/updatecheck.py's
        # own tests) means no persisted cooldown is available, not an error.
        if store is None:
            return None
        enrichment_state = getattr(store, 'enrichment_state', None)
        return None if enrichment_state is None else cls(enrichment_state)

    def integration_cooldown(self, provider):
        if self._enrichment_state is None:
            return None
        return self._enrichment_state.integration_cooldown(provider)

    def defer_integration(self, provider, *, now, retry_after, error):
        if self._enrichment_state is None:
            return None
        return self._enrichment_state.defer_integration(provider, now=now, retry_after=retry_after, error=error)

    def clear_integration_cooldown(self, provider, admitted_deadline):
        if self._enrichment_state is None:
            return None
        return self._enrichment_state.clear_integration_cooldown(provider, admitted_deadline)


class CredentialedAccessState(AccessState):
    """Cooldown state and an explicit current-credential admission capability."""

    __slots__ = ('credential',)

    def __init__(self, enrichment_state, credential):
        super().__init__(enrichment_state)
        self.credential = credential

    @classmethod
    def for_store(cls, store, credential):
        if store is None:
            return None
        return cls(getattr(store, 'enrichment_state', None), credential)


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
