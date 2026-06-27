"""Connectivity circuit breaker shared by the enrichment providers.

Each provider (MusicBrainz, Discogs, Last.fm) talks to a single host. When that host goes
unreachable (DNS failure, refused/reset connection, timeout) every track in a playlist fails the
same way, so plowing through the whole list just burns one pace-interval per track for nothing. Each
provider keeps a CircuitBreaker: its low-level fetch reports the outcome of every HTTP attempt, and
the enrich loop stops once enough *consecutive* unreachable failures pile up. Crucially, any server
response (even a 4xx/5xx) or a successful read clears the streak: the host is reachable, so a miss
is a per-track miss, not an outage.
"""
import urllib.error

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
    """Counts consecutive unreachable-host failures and trips once they reach `threshold`."""

    def __init__(self, threshold=DEFAULT_THRESHOLD):
        self.threshold = threshold
        self.consecutive = 0

    def reset(self):
        """Clear the streak, called at the start of each run so a past outage doesn't pre-trip it."""
        self.consecutive = 0

    def record(self, exc=None):
        """Report one HTTP attempt: `exc` is the exception it raised, or None on success."""
        if exc is not None and is_unreachable(exc):
            self.consecutive += 1
        else:
            self.consecutive = 0          # reachable (success or server error), streak broken

    def tripped(self):
        return self.consecutive >= self.threshold
