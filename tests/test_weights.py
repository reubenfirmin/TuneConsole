from fastapi.testclient import TestClient

from yt_playlist.rec import surfaces
from yt_playlist.web.app import create_app


def test_nudge_clamps_and_shrinks(store):
    import pytest
    store.identities.upsert_identity("main", "cred", None, True)
    w = store.rec_model.nudge_weight("lane:deep_cut", 0.5)
    assert 0.2 <= w < 1.0                       # moved down, within clamp
    store.rec_model.set_weight("lane:x", 2.0, now=1000.0)
    assert store.rec_model.get_weights(now=1000.0)["lane:x"] == pytest.approx(2.0)
    store.rec_model.reset_weights()
    assert store.rec_model.get_weights() == {}


def test_dismiss_with_lane_downweights_that_lane(store):
    store.identities.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.post("/recs/feedback", data={"item": "x|y", "kind": "dismiss", "lane": "deep_cut"})
    # #85 read at the same fixed `now` the route nudged with, else read-time reversion (vs real
    # wall-clock time) would erase the nudge entirely.
    assert store.rec_model.get_weights(now=1.0).get("lane:deep_cut", 1.0) < 1.0


def test_for_you_prefers_higher_weighted_lane(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    day = 86400.0; now = 200 * day
    # a deep cut (deep_cut lane): artist played, plus a neglected track
    hit = store.tracks.upsert_track("h", "Hit", "Fav", None, None)
    store.tracks.upsert_track("b", "Bench", "Fav", None, None)
    # a rotation neighbour (rotation lane): shares a playlist with your most-played, barely played
    nb = store.tracks.upsert_track("n", "Neighbour", "Other", None, None)
    store.playlists.set_playlist_tracks(store.playlists.upsert_playlist(iid, "PL", "Mix", 2, "h", 0.0), [hit, nb])
    store.history.add_history_snapshot(iid, now - 2 * day, ["hit|fav"])
    store.history.add_history_snapshot(iid, now - 1 * day, ["hit|fav"])

    store.rec_model.set_weight("lane:deep_cut", 3.0, now=now)      # strongly prefer the deep-cut lane over rotation
    top = surfaces.for_you(store, now=now, limit=1)
    assert top and top[0].lane == "deep_cut"
