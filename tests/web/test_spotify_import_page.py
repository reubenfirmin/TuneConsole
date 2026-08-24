from fastapi.testclient import TestClient

from yt_playlist.providers.spotify import ImportPlaylist, SpotifyError
from yt_playlist.providers.spotify import ImportTrack
from yt_playlist.library.importing import ExistingOverlap, ImportResolution
from yt_playlist.web.app import create_app


def _client(store):
    iid = store.upsert_identity("main", "cred", None, True)
    store.set_setting("spotify_client_id", "cid")
    store.set_setting("spotify_refresh_token", "refresh")
    store.set_setting("spotify_access_token", "access")
    store.set_setting("spotify_access_expires_at", "9999")
    return TestClient(create_app(store, lambda: {}, now_fn=lambda: 1000.0),
                      base_url="http://127.0.0.1:8123"), iid


def test_playlist_chooser_is_transient_and_targets_youtube_identity(store, monkeypatch):
    client, iid = _client(store)

    class Fake:
        def playlists(self): return [ImportPlaylist("sp1", "Road songs", 12, "Listener")]

    monkeypatch.setattr("yt_playlist.web.routes.spotify_import.import_client", lambda *a: Fake())
    before = store.conn.total_changes
    response = client.get("/spotify/import")
    assert response.status_code == 200
    assert "Road songs" in response.text and 'value="sp1"' in response.text
    assert f'value="{iid}"' in response.text
    assert store.conn.total_changes == before  # Spotify catalog data was not written anywhere


def test_disconnected_redirects_to_setup(store):
    client = TestClient(create_app(store, lambda: {}, now_fn=lambda: 1000.0),
                        base_url="http://127.0.0.1:8123")
    response = client.get("/spotify/import", follow_redirects=False)
    assert response.status_code == 303 and "tab=import" in response.headers["location"]


def test_quota_exhaustion_is_named(store, monkeypatch):
    client, _ = _client(store)

    class Fake:
        def playlists(self): raise SpotifyError(429, "quota", reason="QUOTA_EXCEEDED")

    monkeypatch.setattr("yt_playlist.web.routes.spotify_import.import_client", lambda *a: Fake())
    response = client.get("/spotify/import")
    assert response.status_code == 200 and "quota is exhausted" in response.text


def test_preview_keeps_spotify_rows_in_short_lived_memory(store, monkeypatch):
    client, iid = _client(store)

    class Spotify:
        def playlists(self): return [ImportPlaylist("sp1", "Road songs", 1)]
        def playlist_tracks(self, pid):
            assert pid == "sp1"
            return [ImportTrack("track1", "Song", "Artist", "Album", 200)]

    class YouTube:
        def search(self, query, filter="songs"):
            return [{"videoId": "yt1", "title": "Song", "artists": [{"name": "Artist"}],
                     "duration_seconds": 200}]

    client.app.state.ctx.client_provider = lambda: {iid: YouTube()}
    monkeypatch.setattr("yt_playlist.web.routes.spotify_import.import_client", lambda *a: Spotify())
    before = store.conn.total_changes
    response = client.post("/spotify/import/preview",
                           data={"playlist": "sp1", "target_identity": str(iid)})
    assert response.status_code == 200 and "Matched" in response.text and "yt1" not in response.text
    assert store.conn.total_changes == before
    preview = next(iter(client.app.state.ctx.spotify_imports.values()))
    assert preview["playlists"][0]["rows"][0].target_video_id == "yt1"


def test_confirm_consumes_preview_creates_youtube_playlist_and_audits(store):
    client, iid = _client(store)
    source = ImportTrack("sp", "Song", "Artist", "Album", 200)
    matched = ImportResolution(source, "matched", "yt1", "Song", "Artist", 1.0)
    missed = ImportResolution(source, "unmatched", None, None, None, None)
    client.app.state.ctx.spotify_imports["once"] = {
        "created_at": 1000, "target_identity": iid,
        "playlists": [{"source_id": "sp1", "name": "Road songs", "rows": [matched, missed]}],
    }

    class YouTube:
        def __init__(self): self.created = []; self.added = []
        def create_playlist(self, name, description):
            self.created.append((name, description)); return "PLNEW"
        def add_playlist_items(self, pid, ids): self.added.append((pid, list(ids)))

    youtube = YouTube()
    client.app.state.ctx.client_provider = lambda: {iid: youtube}
    response = client.post("/spotify/import/confirm", data={"token": "once"})
    assert response.status_code == 200 and "<strong>1</strong> track added" in response.text
    assert youtube.created == [("Road songs", "Imported from Spotify by TuneConsole")]
    assert youtube.added == [("PLNEW", ["yt1"])]
    assert "once" not in client.app.state.ctx.spotify_imports
    action = store.get_actions()[0]
    assert action.kind == "copy_playlist" and "Spotify: Road songs" in action.params_json
    assert "track1" not in action.params_json  # no Spotify track/catalog payload in the audit

    replay = client.post("/spotify/import/confirm", data={"token": "once"}, follow_redirects=False)
    assert replay.status_code == 303


def test_confirm_recovers_when_one_matched_item_poisons_batch(store):
    client, iid = _client(store)
    source = ImportTrack("sp", "Song", "Artist", None, 200)
    rows = [ImportResolution(source, "matched", vid, "Song", "Artist", 1.0)
            for vid in ("good", "bad")]
    client.app.state.ctx.spotify_imports["token"] = {
        "created_at": 1000, "target_identity": iid,
        "playlists": [{"source_id": "sp1", "name": "Mix", "rows": rows}],
    }

    class YouTube:
        def create_playlist(self, *a): return "PL"
        def add_playlist_items(self, pid, ids):
            if "bad" in ids: raise RuntimeError("unavailable")

    client.app.state.ctx.client_provider = lambda: {iid: YouTube()}
    response = client.post("/spotify/import/confirm", data={"token": "token"})
    assert response.status_code == 200
    assert "<strong>1</strong> track added" in response.text
    assert "1 matched track rejected" in response.text


def test_exact_duplicate_is_skipped_by_default(store):
    client, iid = _client(store)
    source = ImportTrack("sp", "Song", "Artist", None, 200)
    row = ImportResolution(source, "matched", "yt", "Song", "Artist", 1.0)
    overlap = ExistingOverlap(7, "Already here", 1.0, True, 1, 0, 0)
    client.app.state.ctx.spotify_imports["exact"] = {
        "created_at": 1000, "target_identity": iid,
        "playlists": [{"source_id": "sp1", "name": "Mix", "rows": [row],
                       "overlaps": [overlap]}],
    }

    class YouTube:
        def create_playlist(self, *a): raise AssertionError("must skip")

    client.app.state.ctx.client_provider = lambda: {iid: YouTube()}
    response = client.post("/spotify/import/confirm", data={"token": "exact"})
    assert response.status_code == 200 and "no duplicate playlist was created" in response.text
    assert store.get_actions() == []


def test_near_duplicate_can_add_only_missing_tracks(store):
    client, iid = _client(store)
    existing_track = store.upsert_track("old", "Old", "Artist", None, 200)
    pid = store.upsert_playlist(iid, "PLEXIST", "Existing", 1, "h", 1)
    store.set_playlist_tracks(pid, [existing_track])
    old = ImportTrack("sp-old", "Old", "Artist", None, 200)
    new = ImportTrack("sp-new", "New", "Artist", None, 200)
    rows = [ImportResolution(old, "matched", "old", "Old", "Artist", 1.0),
            ImportResolution(new, "matched", "new", "New", "Artist", 1.0)]
    overlap = ExistingOverlap(pid, "Existing", .5, False, 1, 1, 0)
    client.app.state.ctx.spotify_imports["near"] = {
        "created_at": 1000, "target_identity": iid,
        "playlists": [{"source_id": "sp1", "name": "Mix", "rows": rows,
                       "overlaps": [overlap]}],
    }

    class YouTube:
        def __init__(self): self.added = []
        def add_playlist_items(self, playlist, ids): self.added.append((playlist, list(ids)))
        def create_playlist(self, *a): raise AssertionError("must merge")

    youtube = YouTube()
    client.app.state.ctx.client_provider = lambda: {iid: youtube}
    response = client.post("/spotify/import/confirm",
                           data={"token": "near", "decision": f"0|merge|{pid}"})
    assert response.status_code == 200
    assert youtube.added == [("PLEXIST", ["new"])]
    assert "Added missing tracks to <strong>Existing</strong>" in response.text
