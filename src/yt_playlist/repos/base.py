"""Shared base for the per-domain DAOs split out of the former Store god class.

Each DAO is a thin class scoped to one domain (overlaps, playlists, …). They all share the
Store's single sqlite connection and re-entrant lock: sqlite is not concurrency-safe and FastAPI
serves sync routes from a threadpool, so every DB call serializes on that one lock.
"""
from functools import wraps

from yt_playlist.core.activity import check_cancellable_work
from yt_playlist.core.db_diagnostics import database_operation

# Default display group for generated playlists. Policy lives in playlist_provenance;
# changing a group never changes taste eligibility. Only explicit adoption lifts quarantine.
#
# TWO TIERS OF SIGNAL. Conflating them is the trap, in both directions:
#
#   TRACK-LEVEL taste signal (groupings, analysis, scores, Trends, insights)
#     QUARANTINED. A play sourced from a generated playlist is the app's own recommendation coming
#     back, not a preference the user expressed. Exclude it. Trends violated this for its whole life
#     because the rule was only ever a comment; tests/test_generated_quarantine.py now enforces it.
#
#   MODE-LEVEL selection signal (rec_mode_picks, rec_mode_impressions, ledger_mode_plays)
#     RETAINED DELIBERATELY. Each generated playlist is built from one taste mode, so choosing to play
#     it is weak evidence for THAT MODE at the top of the model stack (it feeds the Thompson sampler
#     via rec.mode_eval.mode_bandit_stats). It is evidence about the mode, never about the tracks.
#     Do not "fix" this tier by quarantining it: that deletes the only non-circular signal we have.
GENERATED_GROUP = "Generated"

# Prefer the main account's recorded rating, including confirmed writes before LM has synced.
# Otherwise use cached Liked Music membership. The outer query must alias tracks as `t`.
LIKED_EXISTS = ("COALESCE((SELECT r.status='LIKE' FROM item_ratings r "
                "JOIN identities i ON i.id=r.identity_id WHERE i.active=1 AND i.is_master=1 "
                "AND r.video_id=t.video_id), EXISTS(SELECT 1 FROM playlist_tracks lpt "
                "JOIN playlists lpl ON lpl.id = lpt.playlist_id "
                "JOIN tracks lt ON lt.id = lpt.track_id "
                "WHERE lpl.ytm_playlist_id = 'LM' AND lt.video_id = t.video_id))")


def synchronized(method):
    """Serialize a DAO method on the shared connection's re-entrant lock.

    The lock releases between calls, so long network-bound work in callers never blocks the DB.
    """
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with database_operation(method.__qualname__), self._lock:
            check_cancellable_work()
            return method(self, *args, **kwargs)
    return wrapper


def snapshot_read(method):
    """Run a substantial read on a private snapshot, without the serving lock.

    Use only for read-only operations. Nested calls reuse the same pinned reader.
    """
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with database_operation(method.__qualname__):
            if getattr(self._store, "_read_only", False):
                with self._lock:
                    check_cancellable_work()
                    return method(self, *args, **kwargs)
            with self._store.read_snapshot() as snapshot:
                reader = type(self)(snapshot.store)
                return wrapper(reader, *args, **kwargs)
    return wrapper


class Repo:
    """Base for domain DAOs: bind the Store's connection + lock (constructor injection)."""
    def __init__(self, db):
        self._store = db
        self.conn = db.conn
        self._lock = db._lock
