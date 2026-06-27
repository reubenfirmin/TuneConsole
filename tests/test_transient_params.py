from yt_playlist.rec import rec_params, recommend, transient
from yt_playlist.core.store import Store
from yt_playlist.util.matching import identity_key


def _store_with_play_history(genre="Techno", n=10, now=1000.0):
    """A fresh store whose recent history is n distinct tracks of one genre (a dominant family).
    History dedupes by key, so distinctness (not repeats) is what builds a facet's play lean."""
    s = Store(":memory:")
    s.init_schema()
    iid = s.identities.upsert_identity("me", "cred", None, True)
    keys = []
    for i in range(n):
        tid = s.upsert_track(f"v{i}", f"song{i}", "band", None, None)
        s.set_track_genre(tid, genre)
        keys.append(identity_key(f"song{i}", "band"))
    s.add_history_snapshot(iid, now, keys)
    return s, now


def test_transient_constants_present():
    assert rec_params.MOOD_RECENCY_ALPHA == 0.35
    assert rec_params.STALE_DECAY_HALFLIFE_D == 3
    assert rec_params.PLAY_TRANSIENT_W == 0.30
    assert rec_params.DISLIKE_TRANSIENT_W == 1.50
    assert rec_params.RECENT_PLAY_LIMIT == 50
    assert rec_params.FACET_GAIN == 0.35
    assert rec_params.FACET_MULT_MIN == 0.35 and rec_params.FACET_MULT_MAX == 2.5
    assert rec_params.DISLIKE_SUPPRESS_DAYS == 365
    assert rec_params.THEME_THRESHOLD == 1.2
    assert rec_params.GRADUATE_UP == 1.05 and rec_params.GRADUATE_DOWN == 0.95
    assert rec_params.SYNC_STALE_S == 24 * 3600
    assert recommend.SYNC_STALE_S == rec_params.SYNC_STALE_S      # back-compat re-export


def test_play_facet_leans_isolates_plays():
    # Recent history dominated by one genre -> that family carries the largest play lean.
    s, now = _store_with_play_history("Techno", n=10)
    leans = transient.play_facet_leans(s, now)
    assert leans, "expected at least one played facet"
    assert all(v > 0 for v in leans.values()), "plays only ever push positive"
    top = max(leans, key=leans.get)
    assert top.startswith("genre:")


def test_play_facet_leans_empty_without_history():
    # No history snapshot -> no recent plays -> no play leans (the exposure funnel's off-switch).
    s = Store(":memory:")
    s.init_schema()
    assert transient.play_facet_leans(s, 1000.0) == {}
