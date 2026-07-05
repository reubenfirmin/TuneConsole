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
    # #85: this used to assert no wall-clock decay; now a single event fades on the clock (mood_halflife_d)
    # but its unit direction never hits exactly zero magnitude, so the tilt stays defined 30d out.
    store2_tilt = transient.centroid_tilt(store, 1001.0 + 30 * 86400, V, idx)
    assert store2_tilt is not None


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


# #85: test_staleness_factor and test_staleness_uses_most_recent_of_either_sync removed here -
# staleness_factor is deleted from transient.py entirely (no sync-freshness relax; every event decays
# on its own wall clock instead). The sync-freshness behavior they covered is Task 4's territory
# (scoring.py / taste_viz.py), not reintroduced in transient.py.


# --- #93v2: machine-queued radio plays are not taste evidence in the transient/graduation funnel
# either (transient.radio_list_ids). Radio-provenance plays must produce NO play lean; organic plays
# still do. ---

def test_radio_provenance_plays_produce_no_play_lean(store):
    k = _jazz_track(store, "v1", "RadioSong", artist="RadioBand")
    iid = store.upsert_identity("main", "c", None, True)
    store.set_setting("radio_playlist_ytm", "PLRADIO")
    store.record_play_event(iid, k, "v1", 5.0, playlist_ytm_id="PLRADIO")
    assert transient.play_facet_leans(store, now=10.0) == {}
    # And the blended facet_leans view sees no play push either (no other sources are seeded).
    assert transient.facet_leans(store, now=10.0) == {}


def test_organic_plays_still_produce_play_lean(store):
    k = _jazz_track(store, "v1", "OrganicSong", artist="OrganicBand")
    iid = store.upsert_identity("main", "c", None, True)
    store.set_setting("radio_playlist_ytm", "PLRADIO")
    store.record_play_event(iid, k, "v1", 5.0, playlist_ytm_id=None)   # no provenance = user-driven
    leans = transient.play_facet_leans(store, now=10.0)
    assert leans.get("artist:OrganicBand", 0.0) > 0


def test_radio_play_lean_stays_zero_after_history_sync_shadow(store):
    # The slow-burn re-entry: radio plays a track today, the next YTM history sync re-records it as
    # a provenance-free noon-bucket history row the SAME day. The lean must stay zero, or radio
    # plays graduate into permanent weights one sync later.
    k = _jazz_track(store, "v1", "RadioSong", artist="RadioBand")
    iid = store.upsert_identity("main", "c", None, True)
    store.set_setting("radio_playlist_ytm", "PLRADIO")
    day = 100 * 86400
    store.record_play_event(iid, k, "v1", day + 61000, playlist_ytm_id="PLRADIO")
    store.record_history_plays(iid, day + 70000, [k])          # the post-sync shadow
    assert transient.play_facet_leans(store, now=day + 80000) == {}
