"""Sync job streams, failures and identity library reconciliation."""
import json
from fastapi.testclient import TestClient
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, _track

def test_dashboard_and_sync(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    client = FakeClient(
        playlists=[{"playlistId": "PL1", "title": "Mix", "count": 1}],
        tracks={"PL1": [_track("v1", "A", "X")]})
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 1000.0)
    c = TestClient(app, base_url="http://127.0.0.1")

    assert c.get("/").status_code == 200
    # /sync now starts a background job and streams progress over SSE
    r = c.post("/sync")
    assert r.status_code == 200
    jid = r.json()["job_id"]
    with c.stream("GET", f"/sync/events/{jid}") as s:   # reading to EOF waits for the job to finish
        body = "".join(s.iter_text())
    assert '"type": "end"' in body
    assert len(store.playlists.get_playlists()) == 1

def test_sync_streams_progress_events(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    client = FakeClient(
        playlists=[{"playlistId": "PL1", "title": "Roadtrip", "count": 1}],
        tracks={"PL1": [{**_track("v1", "Song", "Artist"), "likeStatus": "INDIFFERENT"}]})
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    jid = c.post("/sync").json()["job_id"]
    with c.stream("GET", f"/sync/events/{jid}") as s:
        body = "".join(s.iter_text())
    assert "Roadtrip" in body            # per-playlist progress line streamed
    assert "sync complete" in body       # final done event
    assert '"type": "end"' in body       # stream terminator
    assert len(store.playlists.get_playlists()) == 1

def test_sync_events_unknown_job_404(store):
    app = create_app(store, lambda: {}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    assert c.get("/sync/events/99999").status_code == 404

def test_sync_reports_failure_in_stream(store):
    iid = store.identities.upsert_identity("main", "cred", None, True)
    class Boom(FakeClient):
        def get_library_playlists(self, limit=None): raise RuntimeError("api down")
    app = create_app(store, lambda: {iid: Boom()}, now_fn=lambda: 1.0)
    c = TestClient(app, base_url="http://127.0.0.1")
    jid = c.post("/sync").json()["job_id"]
    with c.stream("GET", f"/sync/events/{jid}") as s:
        body = "".join(s.iter_text())
    events = [json.loads(line.removeprefix("data: ")) for line in body.splitlines()
              if line.startswith("data: ")]
    assert any(event["type"] == "err" and "api down" in event["text"] for event in events)
    terminal = events[-1]
    assert terminal["type"] == "end" and terminal["outcome"] == "partial"
    assert terminal["error"]
    resources = terminal["result"]["accounts"][str(iid)]["resources"]
    assert resources["playlist_listing"]["status"] == "failed"
    assert "api down" in resources["playlist_listing"]["message"]
    assert resources["history"]["status"] == resources["saved_albums"]["status"] == "complete"
    assert store.settings.get_setting("last_sync_at") is None


def test_sync_keeps_playlists_when_library_comes_back_empty(store):
    # Regression: an empty get_library_playlists (session glitch, not a 401) must NOT prune the store.
    from yt_playlist.library import sync as sync_mod
    iid = store.identities.upsert_identity("main", "cred", None, True)
    a = store.playlists.upsert_playlist(iid, "PL1", "Keep Me", 1, "h", 1.0)
    store.playlists.set_playlist_tracks(a, [store.tracks.upsert_track("v0", "S", "X", None, None, 1)])
    sync_mod.sync_identity(store, iid, FakeClient(playlists=[], tracks={}, history=[]), now=2.0)
    assert [p.title for p in store.playlists.get_playlists()] == ["Keep Me"]      # preserved, not wiped


def test_sync_flags_reauth_when_known_identity_returns_empty(store):
    # An identity that HAD playlists but now returns empty is flagged for re-auth (not silently empty).
    from yt_playlist.library import sync as sync_mod
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.playlists.upsert_playlist(iid, "PL1", "Keep Me", 1, "h", 1.0)
    expired = {}
    sync_mod.sync_identity(store, iid, FakeClient(playlists=[], tracks={}, history=[]), now=2.0,
                           label="main", on_auth_expired=lambda i, label: expired.__setitem__(i, label))
    assert expired == {iid: "main"}                    # flagged for re-authentication
    assert [p.title for p in store.playlists.get_playlists()] == ["Keep Me"]   # still preserved


def test_sync_prunes_when_library_is_real(store):
    # A genuine, non-empty library still prunes playlists that are actually gone.
    from yt_playlist.library import sync as sync_mod
    iid = store.identities.upsert_identity("main", "cred", None, True)
    store.playlists.upsert_playlist(iid, "GONE", "Deleted Remotely", 0, "h", 1.0)
    client = FakeClient(playlists=[{"playlistId": "PL2", "title": "New", "count": 1}],
                        tracks={"PL2": [_track("v1", "A", "X")]}, history=[])
    sync_mod.sync_identity(store, iid, client, now=2.0)
    assert sorted(p.title for p in store.playlists.get_playlists()) == ["New"]    # GONE pruned, New added


