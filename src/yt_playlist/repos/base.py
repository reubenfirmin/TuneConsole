"""Shared base for the per-domain DAOs split out of the former Store god class.

Each DAO is a thin class scoped to one domain (overlaps, playlists, …). They all share the
Store's single sqlite connection and re-entrant lock — sqlite is not concurrency-safe and FastAPI
serves sync routes from a threadpool, so every DB call serializes on that one lock.
"""
from functools import wraps

# A track is "liked" if its song (identity_key) appears in any "Liked Music" (LM) playlist. Used as a
# correlated subquery in the per-song views; the outer query must alias the tracks table as `t`.
LIKED_EXISTS = ("EXISTS(SELECT 1 FROM playlist_tracks lpt "
                "JOIN playlists lpl ON lpl.id = lpt.playlist_id "
                "JOIN tracks lt ON lt.id = lpt.track_id "
                "WHERE lpl.ytm_playlist_id = 'LM' AND lt.identity_key = t.identity_key)")


def synchronized(method):
    """Serialize a DAO method on the shared connection's re-entrant lock.

    The lock releases between calls, so long network-bound work in callers never blocks the DB.
    """
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class Repo:
    """Base for domain DAOs: bind the Store's connection + lock (constructor injection)."""
    def __init__(self, db):
        self.conn = db.conn
        self._lock = db._lock
