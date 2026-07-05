"""#95: a successful playlist-mutating route pings the extension to reload the YTM tab if it's
sitting on that playlist. Covers the per-playlist detail routes (remove-track/add-tracks/
reorder/rename); a disconnected extension (send_control raises) must never affect the route's own
response, and a FAILED op must not fire the control at all."""
from fastapi.testclient import TestClient

from yt_playlist.web.app import create_app
from tests.conftest import FakeClient


class _StubBridge:
    """Captures every send_control payload; can be told to raise, like a disconnected extension."""
    def __init__(self, raises=False):
        self.raises = raises
        self.calls = []

    def send_control(self, payload):
        if self.raises:
            raise RuntimeError("no extension connected")
        self.calls.append(payload)


def _client(store, provider, bridge):
    app = create_app(store, provider, now_fn=lambda: 1.0, bridge=bridge)
    return TestClient(app, base_url="http://127.0.0.1")


def _seed_three(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Mix", 3, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track(f"v{i}", f"S{i}", "X", "Alb", 200, 1) for i in range(3)])
    fc = FakeClient(tracks={"PL1": [{"videoId": f"v{i}", "setVideoId": f"sv{i}"} for i in range(3)]})
    return iid, a, fc


def test_remove_track_posts_one_refresh_view_with_ytm_id(store):
    iid, a, fc = _seed_three(store)
    bridge = _StubBridge()
    c = _client(store, lambda: {iid: fc}, bridge)

    r = c.post(f"/playlist/{a}/remove-track", data={"video_id": "v1"})

    assert r.status_code == 200 and r.text == ""
    assert bridge.calls == [{"type": "refresh-view", "playlist": "PL1"}]


def test_disconnected_extension_does_not_break_the_route(store):
    # send_control raises when no extension is connected; the route's normal success response must
    # be returned unaffected.
    iid, a, fc = _seed_three(store)
    bridge = _StubBridge(raises=True)
    c = _client(store, lambda: {iid: fc}, bridge)

    r = c.post(f"/playlist/{a}/remove-track", data={"video_id": "v1"})

    assert r.status_code == 200 and r.text == ""
    assert [t["video_id"] for t in store.playlist_tracks_detail(a)] == ["v0", "v2"]


def test_failed_op_does_not_fire_refresh_view(store):
    # A nonexistent playlist -> the op raises ValueError -> a toast, no mutation, and no control frame.
    store.upsert_identity("main", "cred", None, True)
    bridge = _StubBridge()
    c = _client(store, lambda: {}, bridge)

    r = c.post("/playlist/999999/remove-track", data={"video_id": "v1"})

    assert r.status_code == 422
    assert bridge.calls == []


def test_add_tracks_posts_refresh_view(store):
    import json
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "My Mix", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "Song A", "Artist X", "Alb", 200, 1)])
    bridge = _StubBridge()
    c = _client(store, lambda: {iid: FakeClient()}, bridge)

    track = json.dumps({"videoId": "v1", "title": "Song A (Live)", "artist": "Artist X", "duration": 250})
    r = c.post(f"/playlist/{a}/add-tracks", data={"track": track})

    assert r.headers.get("hx-refresh") == "true"
    assert bridge.calls == [{"type": "refresh-view", "playlist": "PL1"}]


def test_rename_posts_refresh_view(store):
    iid = store.upsert_identity("main", "cred", None, True)
    a = store.upsert_playlist(iid, "PL1", "Old Name", 1, "h", 1.0)
    store.set_playlist_tracks(a, [store.upsert_track("v0", "S", "X", "Al", 200, 1)])
    bridge = _StubBridge()
    c = _client(store, lambda: {iid: FakeClient()}, bridge)

    r = c.post(f"/playlist/{a}/rename", data={"title": "New Name"})

    assert r.status_code == 200
    assert bridge.calls == [{"type": "refresh-view", "playlist": "PL1"}]


def test_reorder_posts_refresh_view(store):
    iid, a, fc = _seed_three(store)
    bridge = _StubBridge()
    c = _client(store, lambda: {iid: fc}, bridge)

    r = c.post(f"/playlist/{a}/reorder", data={"video_id": "v2", "before_video_id": "v0"})

    assert r.status_code == 204
    assert bridge.calls == [{"type": "refresh-view", "playlist": "PL1"}]


def test_delete_posts_refresh_view_for_removed_playlist(store, monkeypatch, tmp_path):
    # A YTM tab sitting on the deleted playlist should show it's gone, not a stale tracklist. The
    # local row is removed by the delete, so the control must carry the YTM id captured beforehand.
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid, a, fc = _seed_three(store)
    bridge = _StubBridge()
    c = _client(store, lambda: {iid: fc}, bridge)

    r = c.post("/playlists/delete", data={"ids": str(a)})

    assert r.headers.get("hx-refresh") == "true"
    assert store.get_playlist(a) is None
    assert bridge.calls == [{"type": "refresh-view", "playlist": "PL1"}]


def test_delete_of_system_playlist_hides_only_and_does_not_fire(store, monkeypatch, tmp_path):
    # Undeletable system playlists are only hidden locally: nothing changed on YouTube, so there
    # is nothing to refresh.
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    iid = store.upsert_identity("main", "cred", None, True)
    lm = store.upsert_playlist(iid, "LM", "Liked Music", 1, "h", 1.0)
    store.set_playlist_tracks(lm, [store.upsert_track("v1", "S", "X", "Al", 200, 1)])
    bridge = _StubBridge()
    c = _client(store, lambda: {iid: FakeClient()}, bridge)

    r = c.post("/playlists/delete", data={"ids": str(lm)})

    assert r.headers.get("hx-refresh") == "true"
    assert bridge.calls == []


def _two_identity_move(store):
    i1 = store.upsert_identity("main", "c1", None, True)
    i2 = store.upsert_identity("Alt", "c2", None, False)
    pid = store.upsert_playlist(i1, "PL1", "Mix", 1, "h", 1.0)
    store.set_playlist_tracks(pid, [store.upsert_track("v1", "S", "X", None, None, 1)])
    return i1, i2, pid


def test_move_that_deletes_source_posts_refresh_view(store):
    # A cross-identity MOVE deletes the source playlist on YouTube; a tab sitting on it should
    # reload to show that. The copy destination is brand new, so no refresh fires for it.
    i1, i2, pid = _two_identity_move(store)
    bridge = _StubBridge()
    c = _client(store, lambda: {i1: FakeClient(), i2: FakeClient()}, bridge)

    r = c.post("/move/run", data={"playlist": pid, "target_identity": i2})

    assert r.status_code == 200 and r.text.strip() == ""
    assert bridge.calls == [{"type": "refresh-view", "playlist": "PL1"}]


def test_copy_only_move_does_not_fire_refresh_view(store):
    # copy_only leaves the source untouched on YouTube: nothing a tab could be viewing changed.
    i1, i2, pid = _two_identity_move(store)
    bridge = _StubBridge()
    c = _client(store, lambda: {i1: FakeClient(), i2: FakeClient()}, bridge)

    r = c.post("/move/run", data={"playlist": pid, "target_identity": i2, "copy_only": "1"})

    assert r.status_code == 200 and "Copied" in r.text
    assert bridge.calls == []
