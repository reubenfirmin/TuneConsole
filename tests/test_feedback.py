from fastapi.testclient import TestClient

from yt_playlist import recommend
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def test_feedback_round_trip(store):
    store.upsert_identity("main", "cred", None, True)
    store.record_feedback("for_you", "a|b", "dismiss", now=1.0)
    assert "a|b" in store.suppressed_keys("for_you", now=2.0)
    assert "a|b" not in store.suppressed_keys("suggest", now=2.0)   # surface-scoped


def test_snooze_expires(store):
    store.upsert_identity("main", "cred", None, True)
    store.record_feedback("for_you", "a|b", "not_now", until=100.0, now=1.0)
    assert "a|b" in store.suppressed_keys("for_you", now=50.0)      # before expiry
    assert "a|b" not in store.suppressed_keys("for_you", now=150.0)  # after expiry


def test_mute_artist(store):
    store.upsert_identity("main", "cred", None, True)
    store.record_feedback("for_you", "artist:Coldplay", "mute", now=1.0)
    assert "Coldplay" in store.muted_artists()


def test_for_you_respects_dismissals(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.upsert_track("v1", "Hit", "Fav", None, None)
    store.upsert_track("v2", "Bench", "Fav", None, None)   # deep cut -> surfaces in for_you
    now = 1000.0
    store.add_history_snapshot(iid, now - 100, ["hit|fav"])
    store.add_history_snapshot(iid, now - 50, ["hit|fav"])

    assert "bench|fav" in {i.key for i in recommend.for_you(store, now=now)}
    store.record_feedback("for_you", "bench|fav", "dismiss", now=now)
    assert "bench|fav" not in {i.key for i in recommend.for_you(store, now=now)}


def test_feedback_endpoint_persists(store):
    store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    r = c.post("/recs/feedback", data={"item": "x|y", "surface": "for_you", "kind": "dismiss"})
    assert r.status_code == 200
    assert "x|y" in store.suppressed_keys("for_you", now=2.0)


def test_feedback_endpoint_requires_item(store):
    store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.post("/recs/feedback", data={}).status_code == 422
