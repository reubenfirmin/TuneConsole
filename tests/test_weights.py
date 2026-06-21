from fastapi.testclient import TestClient

from yt_playlist import recommend
from yt_playlist.web.app import create_app


def test_nudge_clamps_and_shrinks(store):
    store.upsert_identity("main", "cred", None, True)
    w = store.nudge_weight("lane:deep_cut", 0.5)
    assert 0.2 <= w < 1.0                       # moved down, within clamp
    store.set_weight("lane:x", 2.0)
    assert store.get_weights()["lane:x"] == 2.0
    store.reset_weights()
    assert store.get_weights() == {}


def test_dismiss_with_lane_downweights_that_lane(store):
    store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    c.post("/recs/feedback", data={"item": "x|y", "kind": "dismiss", "lane": "deep_cut"})
    assert store.get_weights().get("lane:deep_cut", 1.0) < 1.0


def test_for_you_prefers_higher_weighted_lane(store):
    iid = store.upsert_identity("main", "cred", None, True)
    day = 86400.0; now = 200 * day
    # a forgotten gem (resurface lane)
    store.upsert_track("g", "Gem", "G", None, None)
    store.add_history_snapshot(iid, now - 120 * day, ["gem|g"])
    store.add_history_snapshot(iid, now - 119 * day, ["gem|g"])
    # a deep cut (deep_cut lane): artist played, plus a neglected track
    store.upsert_track("h", "Hit", "Fav", None, None)
    store.upsert_track("b", "Bench", "Fav", None, None)
    store.add_history_snapshot(iid, now - 1 * day, ["hit|fav"])

    store.set_weight("lane:resurface", 3.0)     # strongly prefer the resurface lane
    top = recommend.for_you(store, now=now, limit=1)
    assert top and top[0].lane == "resurface"
