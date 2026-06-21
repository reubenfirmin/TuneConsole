from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _seed(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Hit", "Fav", None, None)
    store.upsert_track("v2", "Bench", "Fav", None, None)   # deep cut -> appears in for_you
    now = 1000.0
    store.add_history_snapshot(iid, now - 100, ["hit|fav"])
    store.add_history_snapshot(iid, now - 50, ["hit|fav"])
    return iid, TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now),
                           base_url="http://127.0.0.1")


def test_home_renders_feedback_chips(store):
    _, c = _seed(store)
    html = c.get("/").text
    for chip in ("More like this", "Not now", "Not the vibe", "Wrong era",
                 "Too mainstream", "Mute Fav", "Already know it"):
        assert chip in html


def test_own_it_suppresses_without_taste_penalty(store):
    _, c = _seed(store)
    c.post("/recs/feedback", data={"item": "bench|fav", "surface": "for_you",
                                   "kind": "dismiss", "reason": "own_it", "lane": "deep_cut"})
    assert "bench|fav" in store.suppressed_keys("for_you", now=1001.0)   # suppressed
    assert "lane:deep_cut" not in store.get_weights()                    # but NO penalty


def test_less_nudges_lane_down(store):
    _, c = _seed(store)
    c.post("/recs/feedback", data={"item": "bench|fav", "surface": "for_you",
                                   "kind": "less", "reason": "vibe", "lane": "deep_cut"})
    assert store.get_weights().get("lane:deep_cut", 1.0) < 1.0


def test_mute_artist_via_chip(store):
    _, c = _seed(store)
    c.post("/recs/feedback", data={"item": "artist:Fav", "surface": "for_you", "kind": "mute"})
    assert "Fav" in store.muted_artists()
