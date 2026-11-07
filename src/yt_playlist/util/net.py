"""Request failure reporting and retry classification shared by enrichment providers.

Integration boundaries own provider access and availability. They report request failures through
the probe-local error collector so the waterfall can distinguish outages from confirmed misses.
The waterfall persists provider cooldowns and Retry-After deadlines across batches and restarts.
"""
import urllib.error
import math
from datetime import timezone
from email.utils import parsedate_to_datetime
from contextlib import contextmanager
from contextvars import ContextVar
from yt_playlist.integrations.base import IntegrationUnavailable

_probe_errors = ContextVar("enrichment_probe_errors", default=None)


def record_probe_failure(reason):
    errors = _probe_errors.get()
    if errors is not None:
        errors.append(reason)


def retry_after(exc, now):
    """An HTTP Retry-After deadline, accepting both seconds and an HTTP date."""
    if isinstance(exc, IntegrationUnavailable):
        return exc.retry_at
    headers = getattr(exc, "headers", None)
    value = headers.get("Retry-After") if headers else None
    if value is None:
        return 0
    try:
        seconds = float(value)
        return now + max(0, seconds) if math.isfinite(seconds) else 0
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(now, date.timestamp())
        except (ValueError, TypeError, OverflowError):
            return 0


def provider_outage(exc):
    if isinstance(exc, IntegrationUnavailable):
        return exc.outage
    return (is_unreachable(exc) or isinstance(exc, urllib.error.HTTPError)
            and (exc.code == 429 or exc.code >= 500))


@contextmanager
def probe_errors():
    """Capture request failures within one provider probe, isolated from other threads/jobs.

    A missing recording (404) is a valid answer. Transport errors, throttling, server errors and
    invalid responses need another attempt rather than being recorded as confirmed misses.
    """
    errors = []
    token = _probe_errors.set(errors)
    try:
        yield errors
    finally:
        _probe_errors.reset(token)


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
