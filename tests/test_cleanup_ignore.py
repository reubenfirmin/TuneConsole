"""Category-scoped cleanup ignores, end-to-end through the routes: a per-playlist dismissal (Empty/
Tiny) and a per-merge dismissal (Exact/Near duplicates) both drop the item from the cleanup page AND
the home 'Playlist cleanups' count, and unignoring restores it."""
from fastapi.testclient import TestClient

from yt_playlist.rec import recommend
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


def _app(store):
    iid = store.upsert_identity("main", "cred", None, True)
    return iid, TestClient(create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1000.0),
                           base_url="http://127.0.0.1")


def _count_after_reload(c, store):
    """The home count after the browser's HX-Refresh reload of /cleanup (which re-materializes it)."""
    c.get("/cleanup")                                             # the reload every cleanup edit triggers
    return (store.get_proposals("cleanup") or {}).get("count", 0)


def test_ignore_empty_playlist_round_trip(store):
    iid, c = _app(store)
    store.upsert_playlist(iid, "PE", "Strays", 0, "h", 0.0)        # one empty playlist
    assert _count_after_reload(c, store) == 1
    assert "Strays" in c.get("/cleanup").text

    c.post("/cleanup/ignore", data={"ytm": "PE", "category": "empty"})
    assert _count_after_reload(c, store) == 0                      # gone from the home count
    assert "Ignored cleanups" in c.get("/cleanup").text           # surfaced in the dismissed section
    assert not any(a.kind == "cleanup" for a in recommend.take_action(store, 1001.0, {}))

    c.post("/cleanup/unignore", data={"ytm": "PE", "category": "empty"})
    assert _count_after_reload(c, store) == 1                      # restored


def test_ignore_merge_round_trip(store):
    iid, c = _app(store)
    a = store.upsert_playlist(iid, "PLA", "Mix", 4, "h", 0.0)
    b = store.upsert_playlist(iid, "PLB", "Mix copy", 4, "h", 0.0)
    ts = [store.upsert_track(f"v{i}", f"S{i}", "X", None, 1) for i in range(4)]   # 4 tracks -> not 'tiny'
    store.set_playlist_tracks(a, ts); store.set_playlist_tracks(b, ts)            # identical -> a merge
    assert _count_after_reload(c, store) == 2                      # both copies involved

    c.post("/cleanup/ignore-merge", data={"members": "PLA,PLB"})
    assert _count_after_reload(c, store) == 0                      # the merge (and its playlists) gone
    assert "Ignored cleanups" in c.get("/cleanup").text

    c.post("/cleanup/unignore-merge", data={"signature": "PLA|PLB"})
    assert _count_after_reload(c, store) == 2                      # restored
