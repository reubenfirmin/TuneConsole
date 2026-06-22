from fastapi.testclient import TestClient

from yt_playlist.rec import embed
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    return iid, TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                           base_url="http://127.0.0.1")


def test_songs_like_this_renders_neighbours(store):
    iid, c = _client(store)
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(6)]
    [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(6)]
    store.set_playlist_tracks(store.upsert_playlist(iid, "P", "P", 12, "h", 0.0), A)
    embed.build_and_store(store, dim=4)

    r = c.get("/track/a0/similar")              # a0's video_id
    assert r.status_code == 200
    assert "Songs like" in r.text
    assert "A1" in r.text                        # nearest neighbours rendered
    assert r.text.index("A1") < r.text.index("B0")   # A-cluster ranks above the other cluster


def test_songs_like_unknown_video_renders_empty(store):
    _, c = _client(store)
    r = c.get("/track/nope/similar")
    assert r.status_code == 200                 # graceful: no key -> "no similar tracks yet"
