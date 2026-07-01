import numpy as np
import pytest
from yt_playlist.core.store import Store
from yt_playlist.rec import rec_params, transient
from yt_playlist.util.matching import identity_key


def _jazz_track(store, vid, title, artist="A", year="1960"):
    tid = store.upsert_track(vid, title, artist, None, None, 1)
    store.set_track_genre(tid, "jazz")
    store.set_track_year(tid, year)
    return identity_key(title, artist)


def test_mood_event_moves_facet_lean(store):
    k = _jazz_track(store, "v1", "S")
    store.record_mood([k], -1, now=10.0)                       # "less" this jazz vibe
    leans = transient.facet_leans(store, now=10.0)
    assert any(f.startswith("genre:") and v < 0 for f, v in leans.items())
    assert leans.get("artist:A", 0.0) < 0


def test_recent_play_moves_lean_positive(store):
    k = _jazz_track(store, "v1", "S")
    # Seed a play using the real API (history_items has no played_at column)
    iid = store.upsert_identity("main", "c", None, True)
    store.add_history_snapshot(iid, 5.0, [k])
    leans = transient.facet_leans(store, now=10.0)
    assert leans.get("artist:A", 0.0) > 0                      # a recent play pushes its facets up


def test_dislike_moves_lean_negative(store):
    k = _jazz_track(store, "v1", "S")
    store.record_dislike(k, until=9e9, now=5.0)
    leans = transient.facet_leans(store, now=10.0)
    # #54: a dislike must NOT push the artist negative (one stinker can't mute the whole artist),
    assert leans.get("artist:A", 0.0) >= 0
    # but it still registers as a broad (genre/era) negative signal, so it isn't inert.
    assert any(v < 0 for f, v in leans.items() if not f.startswith("artist:"))


def test_facet_multiplier_clamps_and_neutral_at_zero(store):
    g = rec_params.get_param(store, "facet_gain")
    lo = rec_params.get_param(store, "facet_mult_min")
    hi = rec_params.get_param(store, "facet_mult_max")
    assert transient.facet_multiplier(0.0, g, lo, hi) == 1.0
    assert transient.facet_multiplier(-100.0, g, lo, hi) == lo
    assert transient.facet_multiplier(100.0, g, lo, hi) == hi
    assert transient.facet_multiplier(-1.0, g, lo, hi) < 1.0


def test_facet_multiplier_uses_params(store):
    g = rec_params.get_param(store, "facet_gain")
    lo = rec_params.get_param(store, "facet_mult_min")
    hi = rec_params.get_param(store, "facet_mult_max")
    # default behavior preserved: 1 + gain*lean, clamped
    assert transient.facet_multiplier(0.0, g, lo, hi) == 1.0
    assert transient.facet_multiplier(1.0, g, lo, hi) == max(lo, min(hi, 1.0 + g))
    assert transient.facet_multiplier(-1.0, g, lo, hi) >= lo


def test_centroid_tilt_newest_dominates_and_persists(store):
    V, idx = np.array([[1.0, 0.0]]), {"a|x": 0}
    store.record_mood(["a|x"], 1, now=1000.0)
    store.record_mood(["a|x"], -1, now=1001.0)                 # newest: away
    assert transient.centroid_tilt(store, 1001.0, V, idx)[0] < 0
    # persists with no wall-clock decay
    store2_tilt = transient.centroid_tilt(store, 1001.0 + 30 * 86400, V, idx)
    assert store2_tilt is not None


def test_staleness_factor(store):
    store.set_setting("last_sync_at", str(1000.0))
    assert transient.staleness_factor(store, 1000.0 + rec_params.SYNC_STALE_S) == 1.0
    half = transient.staleness_factor(store, 1000.0 + rec_params.SYNC_STALE_S + 3 * 86400)
    assert abs(half - 0.5) < 1e-6


def test_centroid_tilt_includes_recent_plays():
    s = Store(":memory:")
    s.init_schema()
    iid = s.upsert_identity("m", "c", None, True)
    s.upsert_track("v1", "s", "band", None, None)
    s.add_history_snapshot(iid, 1.0, ["s|band"])
    V = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float64)
    idx = {"s|band": 0, "other|x": 1}
    tilt = transient.centroid_tilt(s, 1000.0, V, idx)
    assert tilt is not None
    assert tilt[0] > tilt[1]     # leans toward the played track's direction


def test_staleness_uses_most_recent_of_either_sync(store):
    # Regression: freshness/decay read only last_sync_at (full sync), so a recent quick auto-sync
    # (last_plays_sync_at) still read as stale. It must use the most recent of EITHER, like sync_status.
    from yt_playlist.rec import transient
    now = 1_000_000.0
    store.set_setting("last_sync_at", str(now - 10 * 86400))     # full sync 10 days ago
    assert transient.staleness_factor(store, now) < 1.0          # stale on its own
    store.set_setting("last_plays_sync_at", str(now - 600))      # auto-synced 10 min ago
    assert transient.staleness_factor(store, now) == 1.0         # fresh again
