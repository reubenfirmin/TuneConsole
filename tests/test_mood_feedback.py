"""Transient mood feedback: a decaying tilt on the Home lanes, not a permanent taste signal."""
import numpy as np
from fastapi.testclient import TestClient

from yt_playlist import recommend
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def test_mood_records_and_persists(store):
    store.record_mood(["a|x", "b|x"], 1, now=1000.0)
    ev = store.recent_mood_events()
    assert len(ev) == 1 and ev[0][1] == 1 and ev[0][2] == ["a|x", "b|x"]
    assert len(store.recent_mood_events()) == 1                 # still there later (no purge)


def test_mood_tilt_points_toward_seed(store):
    V, idx = np.array([[1.0, 0.0], [0.0, 1.0]]), {"a|x": 0, "b|x": 1}
    store.record_mood(["a|x"], 1, now=1000.0)
    tilt = recommend.mood_tilt(store, 1000.0, V, idx)
    assert tilt is not None and tilt[0] > 0.9                  # leans toward the seed's vibe


def test_mood_tilt_points_away_when_negative(store):
    V, idx = np.array([[1.0, 0.0], [0.0, 1.0]]), {"a|x": 0, "b|x": 1}
    store.record_mood(["a|x"], -1, now=1000.0)
    tilt = recommend.mood_tilt(store, 1000.0, V, idx)
    assert tilt is not None and tilt[0] < -0.9                 # leans away


def test_mood_endpoint_records_and_confirms(store):
    iid = store.upsert_identity("main", "cred", None, True)
    t = store.upsert_track("v0", "S0", "Art", None, None, 1)
    pid = store.upsert_playlist(iid, "PLG", "From your catalog - June 21 2026", 1, "h", 1.0)
    store.set_playlist_tracks(pid, [t])
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")

    r = c.post("/recs/mood", data={"pid": pid, "dir": 1})
    # No swap now — the panel stays put so Advanced is reachable; the choice persists via mood state.
    assert r.status_code == 200 and r.text == ""
    assert len(store.recent_mood_events()) == 1             # the mood was recorded


def test_recs_mood_accepts_key_subset_and_intensity(store):
    import json
    iid = store.upsert_identity("main", "cred", None, True)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0),
                   base_url="http://127.0.0.1")
    # facet/track lever: tilt AWAY from a specific subset, "a lot" -> magnitude 2
    r = c.post("/recs/mood", data={"keys": json.dumps(["techno1|x", "techno2|x"]),
                                   "dir": "-1", "intensity": "lot"})
    assert r.status_code == 200
    ev = store.recent_mood_events()
    assert len(ev) == 1
    assert ev[0][1] == -2 and ev[0][2] == ["techno1|x", "techno2|x"]   # signed magnitude, exact subset


def test_recs_mood_whole_playlist_still_works(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("v1", "A", "X", None, None)
    pid = store.upsert_playlist(iid, "PL", "Mix", 1, "h", 0.0)
    store.set_playlist_tracks(pid, [a])
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0),
                   base_url="http://127.0.0.1")
    r = c.post("/recs/mood", data={"pid": pid, "dir": "1"})      # simple whole-mix path (no keys)
    assert r.status_code == 200
    ev = store.recent_mood_events()
    assert len(ev) == 1 and ev[0][1] == 1 and ev[0][2] == ["a|x"]


def test_playlist_mood_state_reflects_active_whole_mix_mood(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_track("v1", "A", "X", None, None)
    pid = store.upsert_playlist(iid, "PL", "Mix", 1, "h", 0.0)
    store.set_playlist_tracks(pid, [a])
    keys = store.get_playlist_track_keys(pid)
    assert recommend.playlist_mood_state(store, pid, now=1000.0) == 0     # nothing yet
    store.record_mood(keys, 1, now=1000.0)
    assert recommend.playlist_mood_state(store, pid, now=1000.0) == 1     # remembered: more
    store.record_mood(keys, -1, now=1001.0)
    assert recommend.playlist_mood_state(store, pid, now=1001.0) == -1    # latest wins: less


def test_track_mood_states_flags_per_track_levers_only(store):
    # Per-track "more/less like this" seed a single key -> flagged; whole-mix/facet (many keys) don't.
    store.record_mood(["solo|x"], 1, now=1000.0)               # 🔥 more on one track
    store.record_mood(["down|x"], -1, now=1000.0)              # 🙅 less on one track
    store.record_mood(["m1|x", "m2|x"], 1, now=1000.0)         # whole-mix / facet tilt (multi-key)
    states = recommend.track_mood_states(store, now=1000.0)
    assert states == {"solo|x": 1, "down|x": -1}               # only the single-key per-track levers
    assert "m1|x" not in states and "m2|x" not in states

    store.record_mood(["solo|x"], -1, now=1001.0)              # flip it: latest wins
    assert recommend.track_mood_states(store, now=1001.0)["solo|x"] == -1
