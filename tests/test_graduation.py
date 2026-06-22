# tests/test_graduation.py
import json
import pytest
from yt_playlist.rec import recommend
from yt_playlist.util.matching import identity_key
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def test_graduation_crosses_threshold_and_resets(store):
    tid = store.upsert_track("v1", "S", "Coltrane", None, None, 1)
    store.set_track_genre(tid, "jazz")
    store.set_track_year(tid, "1960")
    k = identity_key("S", "Coltrane")
    recommend.graduate_moods(store, [k], 1, now=1.0)            # +1.0 each facet, below 1.2
    assert store.get_weights().get("artist:Coltrane", 1.0) == 1.0
    recommend.graduate_moods(store, [k], 1, now=2.0)           # total 2.0 -> graduate
    w = store.get_weights()
    assert w.get("artist:Coltrane", 1.0) > 1.0
    assert w.get("era:1960", 1.0) > 1.0
    assert any(ax.startswith("genre:") and v > 1.0 for ax, v in w.items())
    assert store.get_theme("artist:Coltrane") == pytest.approx(0.8)   # 2.0 - 1.2 remainder


def test_presence_weighting_tames_diffuse_artist(store):
    keys = []
    for i in range(20):
        tid = store.upsert_track(f"v{i}", f"S{i}", f"Art{i}", None, None, 1)
        store.set_track_genre(tid, "jazz")
        store.set_track_year(tid, "1965")
        keys.append(identity_key(f"S{i}", f"Art{i}"))
    for t in range(3):
        recommend.graduate_moods(store, keys, 1, now=float(t))
    w = store.get_weights()
    assert any(ax.startswith("genre:") and v > 1.0 for ax, v in w.items())
    assert all(not ax.startswith("artist:") or v == 1.0 for ax, v in w.items())


def test_graduation_never_suppresses(store):
    tid = store.upsert_track("v1", "S", "Coltrane", None, None, 1)
    store.set_track_genre(tid, "jazz")
    k = identity_key("S", "Coltrane")
    for t in range(5):
        recommend.graduate_moods(store, [k], 1, now=float(t))
    assert k not in store.suppressed_keys("for_you", 100.0)
    assert k not in store.disliked_identity_keys()


def test_recs_mood_route_feeds_graduation(store):
    iid = store.upsert_identity("main", "cred", None, True)
    tid = store.upsert_track("v1", "S", "Coltrane", None, None, 1)
    store.set_track_genre(tid, "jazz")
    pid = store.upsert_playlist(iid, "PL", "Mix", 1, "h", 0.0)
    store.set_playlist_tracks(pid, [tid])
    c = TestClient_factory(store, iid)
    k = identity_key("S", "Coltrane")
    c.post("/recs/mood", data={"keys": json.dumps([k]), "dir": "1", "intensity": "lot"})  # signed 2 -> crosses
    assert store.get_weights().get("artist:Coltrane", 1.0) > 1.0


def TestClient_factory(store, iid):
    from fastapi.testclient import TestClient
    return TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                      base_url="http://127.0.0.1")
