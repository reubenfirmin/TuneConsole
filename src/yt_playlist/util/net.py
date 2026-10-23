"""Connectivity circuit breaker shared by the enrichment providers.

Each provider (MusicBrainz, Discogs, Last.fm) talks to a single host. When that host goes
unreachable (DNS failure, refused/reset connection, timeout) every track in a playlist fails the
same way, so plowing through the whole list just burns one pace-interval per track for nothing. Each
provider keeps a CircuitBreaker: its low-level fetch reports every HTTP attempt. Transport failures,
throttling and server errors count toward an outage; successful reads and valid misses clear it.
The waterfall also persists cooldowns and Retry-After deadlines across batches and restarts.
"""
import urllib.error
from datetime import timezone
from email.utils import parsedate_to_datetime
from contextlib import contextmanager
from contextvars import ContextVar

_probe_errors = ContextVar("enrichment_probe_errors", default=None)


def record_probe_failure(reason):
    errors = _probe_errors.get()
    if errors is not None:
        errors.append(reason)


def retry_after(exc, now):
    """An HTTP Retry-After deadline, accepting both seconds and an HTTP date."""
    headers = getattr(exc, "headers", None)
    value = headers.get("Retry-After") if headers else None
    if value is None:
        return 0
    try:
        return now + max(0, float(value))
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(now, date.timestamp())
        except (ValueError, TypeError, OverflowError):
            return 0


def provider_outage(exc):
    return (is_unreachable(exc) or isinstance(exc, urllib.error.HTTPError)
            and (exc.code == 429 or exc.code >= 500))


@contextmanager
def probe_errors():
    """Capture request failures within one provider probe, isolated from other threads/jobs.

    A missing recording (404) is a valid answer. Transport errors, throttling, server errors and
    invalid responses need another attempt, even before the provider's circuit breaker trips.
    """
    errors = []
    token = _probe_errors.set(errors)
    try:
        yield errors
    finally:
        _probe_errors.reset(token)

DEFAULT_THRESHOLD = 5                     # consecutive unreachable failures before we give up


def is_unreachable(exc):
    """True when `exc` means the host could not be reached at all, vs. a server that answered.

    An HTTPError means the server sent a response, so connectivity is fine. A plain URLError wraps a
    socket-level failure (DNS gaierror, connection refused/reset); a bare OSError/TimeoutError is the
    same class of fault. Anything else (e.g. a JSON parse error) implies we got bytes back: reachable.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        return True
    return isinstance(exc, OSError)


class CircuitBreaker:
    """Counts consecutive outage/throttling failures and trips at `threshold`."""

    def __init__(self, threshold=DEFAULT_THRESHOLD):
        self.threshold = threshold
        self.consecutive = 0

    def reset(self):
        """Clear the streak, called at the start of each run so a past outage doesn't pre-trip it."""
        self.consecutive = 0

    def record(self, exc=None):
        """Report one HTTP attempt: `exc` is the exception it raised, or None on success."""
        if exc is not None:
            if not isinstance(exc, urllib.error.HTTPError) or exc.code != 404:
                record_probe_failure(exc)
        if exc is not None and provider_outage(exc):
            self.consecutive += 1
        else:
            self.consecutive = 0          # a usable response breaks the outage streak

    def tripped(self):
        return self.consecutive >= self.threshold
