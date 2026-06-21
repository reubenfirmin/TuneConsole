from fastapi.testclient import TestClient

from yt_playlist import embed, recommend
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def test_auto_playlists_proposes_unplaylisted_cluster(store):
    iid = store.upsert_identity("main", "cred", None, True)
    A = [store.upsert_track(f"a{i}", f"A{i}", "AB", None, None) for i in range(12)]
    [store.upsert_track(f"b{i}", f"B{i}", "BB", None, None) for i in range(12)]   # no playlist
    store.set_playlist_tracks(store.upsert_playlist(iid, "PA", "PA", 12, "h", 0.0), A)
    embed.build_and_store(store, dim=4)

    props = recommend.auto_playlists(store, k=2, min_size=8)
    assert props                                   # the B cluster (not a playlist) is proposed
    assert all("AB" not in p["label"] for p in props)   # the existing-playlist cluster is excluded
    assert any(t["artist"] == "BB" for p in props for t in p["sample"])


def test_auto_playlists_empty_without_vectors(store):
    store.upsert_identity("main", "cred", None, True)
    assert recommend.auto_playlists(store) == []


def test_home_auto_playlists_fragment_200(store):
    iid = store.upsert_identity("main", "cred", None, True)
    c = TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0),
                   base_url="http://127.0.0.1")
    assert c.get("/home/auto-playlists").status_code == 200
