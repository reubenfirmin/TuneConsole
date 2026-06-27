"""Task 7 (#53): the Tools > Discovery Pools page lists the pools and the add-to-collection (Like)
action likes a discovered track."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    fake = FakeClient()
    return fake, TestClient(create_app(store, lambda: {iid: fake}, now_fn=lambda: 1000.0),
                            base_url="http://127.0.0.1")


def test_discovery_page_lists_pooled_track(store):
    store.upsert_discovered_track("a|x", "v1", "Cold Song", "Artist", "Alb", None, "house", "2020",
                                  "r", 100.0)
    store.mark_offered("track", ["a|x"], 200.0)
    _, c = _client(store)
    r = c.get("/discovery")
    assert r.status_code == 200
    assert "Cold Song" in r.text


def test_discovery_add_likes_track(store):
    store.upsert_discovered_track("a|x", "v1", "Cold Song", "Artist", "Alb", None, None, None, "r", 100.0)
    fake, c = _client(store)
    r = c.post("/discovery/add", data={"kind": "track", "id": "a|x"})
    assert r.status_code == 200
    assert ("v1", "LIKE") in fake.rated      # rated thumbs-up -> Liked Music
