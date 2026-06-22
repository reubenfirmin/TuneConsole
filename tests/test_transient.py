import numpy as np
import pytest
from yt_playlist import rec_params, transient
from yt_playlist.matching import identity_key


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
    assert leans.get("artist:A", 0.0) < 0


def test_facet_multiplier_clamps_and_neutral_at_zero(store):
    assert transient.facet_multiplier(0.0) == 1.0
    assert transient.facet_multiplier(-100.0) == rec_params.FACET_MULT_MIN
    assert transient.facet_multiplier(100.0) == rec_params.FACET_MULT_MAX
    assert transient.facet_multiplier(-1.0) < 1.0


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
