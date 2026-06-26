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
    store.set_setting("last_sync_at", str(now))     # synced -> Home renders the rec feed
    return iid, TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: now),
                           base_url="http://127.0.0.1")


def test_home_proto_curation_not_pre_listen_taste_chips(store):
    # The Home proto-playlists are drafts you curate before listening, so each row offers a plain
    # "remove from this playlist", NOT taste feedback that only makes sense after hearing a track.
    _, c = _seed(store)
    html = c.get("/").text
    assert "gen-rm" in html and "Remove from this playlist" in html
    for chip in ("More like this", "Not the vibe", "Wrong era", "Too mainstream", "Already know it"):
        assert chip not in html


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


def test_feedback_axis_routes_through_graduation_and_eventually_moves_weight(store):
    iid = store.upsert_identity("main", "cred", None, True)
    from fastapi.testclient import TestClient
    from yt_playlist.web.app import create_app
    from tests.conftest import FakeClient
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")

    # Why-chip steering goes through the graduation ledger now (#43 / §4b), not a direct nudge: a single
    # event moves the ledger but leaves the permanent weight neutral (it is below THEME_THRESHOLD).
    c.post("/recs/feedback", data={"item": "a|b", "kind": "less", "axis": "era:1990"})
    assert store.get_weights().get("era:1990", 1.0) == 1.0     # one event: ledger only, weight untouched
    assert (store.get_theme("era:1990") or 0) < 0

    # Sustained feedback crosses the threshold and graduates the weight.
    for _ in range(3):
        c.post("/recs/feedback", data={"item": "a|b", "kind": "less", "axis": "era:1990"})
    assert store.get_weights()["era:1990"] < 1.0               # 'less' graduates it down

    for _ in range(4):
        c.post("/recs/feedback", data={"item": "c|d", "kind": "more", "axis": "artist:Foo"})
    assert store.get_weights()["artist:Foo"] > 1.0            # 'more' graduates it up
