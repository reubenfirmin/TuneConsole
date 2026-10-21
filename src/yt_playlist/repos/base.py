"""Shared base for the per-domain DAOs split out of the former Store god class.

Each DAO is a thin class scoped to one domain (overlaps, playlists, …). They all share the
Store's single sqlite connection and re-entrant lock: sqlite is not concurrency-safe and FastAPI
serves sync routes from a threadpool, so every DB call serializes on that one lock.
"""
from functools import wraps

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

# A track is "liked" if its song (identity_key) appears in any "Liked Music" (LM) playlist. Used as a
# correlated subquery in the per-song views; the outer query must alias the tracks table as `t`.
LIKED_EXISTS = ("EXISTS(SELECT 1 FROM playlist_tracks lpt "
                "JOIN playlists lpl ON lpl.id = lpt.playlist_id "
                "JOIN tracks lt ON lt.id = lpt.track_id "
                "WHERE lpl.ytm_playlist_id = 'LM' AND lt.video_id = t.video_id)")


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
