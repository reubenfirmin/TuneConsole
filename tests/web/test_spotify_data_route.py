import asyncio
import io
import json
import threading
import time
import zipfile

from fastapi.testclient import TestClient

from tests.conftest import FakeClient, _track
from yt_playlist.web.app import create_app


def _payload():
    row = {"ts": "2024-01-01T10:00:00Z", "master_metadata_track_name": "Song",
           "master_metadata_album_artist_name": "Artist", "spotify_track_uri": "spotify:track:x",
           "ms_played": 180000}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("Spotify Extended Streaming History/Streaming_History_Audio_2024.json",
                         json.dumps([row]))
    return out.getvalue()


def _library_payload():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("Spotify Account Data/Playlist1.json", json.dumps({"playlists": [{
            "name": "Imported mix", "items": [{"track": {
                "trackName": "Song", "artistName": "Artist", "albumName": "Album",
                "trackUri": "spotify:track:one"}}, {"track": {
                "trackName": "Missing song", "artistName": "Missing artist",
                "albumName": "Missing record", "trackUri": "spotify:track:missing"}}]}]}))
        archive.writestr("Spotify Account Data/YourLibrary.json", json.dumps({"albums": [{
            "artist": "Missing artist", "album": "Missing album",
            "uri": "spotify:album:missingalbum"}]}))
    return out.getvalue()


def _client(store):
    store.upsert_identity("main", "bridge", None, True)
    store.upsert_track("yt", "Song", "Artist", None, 180)
    return TestClient(create_app(store, lambda: {}, now_fn=lambda: 1000.0),
                      base_url="http://127.0.0.1:8123")


def _wait_for_library_job(store, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = store.get_setting("spotify_library_import_job")
        job = json.loads(raw) if raw else None
        if job and job["status"] not in ("queued", "running"):
            return job
        time.sleep(.01)
    raise AssertionError("Spotify library import did not finish")


def test_setup_offers_local_export_upload_without_oauth(store):
    body = _client(store).get("/setup?tab=import").text
    assert "Import Spotify listening history" in body
    assert "Extended streaming history" in body
    assert "Account data" in body
    assert "playlists, saved songs, albums, and artists" in body
    assert "Import Spotify playlists and albums" in body
    assert "Be prepared to wait several days" in body
    assert "Playlist1.json" in body and "YourLibrary.json" in body
    assert 'hx-post="/import/spotify-library/v4"' in body
    assert "Restart TuneConsole" in body
    assert 'hx-post="/import/spotify"' in body
    assert "Client ID" not in body and "/spotify/connect" not in body


def test_upload_imports_history_and_replaces_form_with_report(store):
    client = _client(store)
    response = client.post("/import/spotify", files={
        "file": ("my_spotify_data.zip", _payload(), "application/zip")})
    assert response.status_code == 200
    assert "Spotify import complete" in response.text
    assert response.headers["HX-Retarget"] == "#spotify-import-block"
    assert store.get_setting("spotify_imported_at") == "1000.0"


def test_unreadable_upload_keeps_form_for_retry(store):
    response = _client(store).post("/import/spotify", files={
        "file": ("wrong.zip", b"not a spotify export", "application/zip")})
    assert response.status_code == 200
    assert "Could not find Spotify Extended Streaming History" in response.text
    assert "HX-Retarget" not in response.headers


def test_account_data_upload_starts_background_job_and_persists_full_dashboard_report(store):
    identity = store.upsert_identity("main", "bridge", None, True)
    client = FakeClient(search_results=[_track("yt-song", "Song", "Artist")])
    web = TestClient(create_app(store, lambda: {identity: client}, now_fn=lambda: 1000.0),
                     base_url="http://127.0.0.1:8123")

    response = web.post("/import/spotify-library/v4", files={
        "file": ("spotify-account-data.zip", _library_payload(), "application/zip")})

    assert response.status_code == 200
    assert response.headers["HX-Retarget"] == "#spotify-library-import-block"
    assert ("running in the background" in response.text
            or "View latest Spotify import report" in response.text)

    job = _wait_for_library_job(store)
    assert job["status"] == "done"
    assert job["report_schema"] == 4
    assert {k: v for k, v in job["report"].items() if k != "details"} == {
        "playlists_found": 1, "playlists_imported": 1, "playlists_skipped": 0,
        "tracks_imported": 1, "tracks_unmatched": 1,
        "albums_found": 1, "albums_imported": 0, "albums_skipped": 0,
        "albums_unmatched": 1,
    }
    assert job["report"]["details"]["playlists"][0]["name"] == "Imported mix"
    assert job["report"]["details"]["playlists"][0]["status"] == "imported"
    assert client.created[0][1] == "Imported mix"
    assert store.get_setting("spotify_library_imported_at") == "1000.0"

    status = web.get("/import/spotify-library/status", headers={"HX-Request": "true"})
    assert status.headers["HX-Redirect"] == "/#notices"
    assert "View latest Spotify import report" in status.text

    import_page = web.get("/setup?tab=import")
    assert import_page.text.count("View latest Spotify import report") == 1
    assert "The report is ready with everything added" not in import_page.text

    store.set_setting("last_sync_at", "999.0")
    dashboard = web.get("/")
    assert "Spotify import complete" in dashboard.text
    assert "Review what was added, skipped, and unmatched" in dashboard.text
    assert 'href="/import/spotify-library/report"' in dashboard.text
    assert "Created in YouTube Music" not in dashboard.text
    assert "panel: location.hash.slice(1) || 'notices'" in dashboard.text

    report = web.get("/import/spotify-library/report")
    assert report.status_code == 200
    assert "What TuneConsole found in the export" in report.text
    assert "distinct YouTube Music lookups" not in report.text
    assert ">Complete<" not in report.text
    assert "1 imported" in report.text and "1 added" in report.text
    assert "Created in YouTube Music</dt><dd>1" in report.text
    assert "Could not be matched</dt><dd>1" in report.text
    assert "Item-by-item audit" in report.text
    assert "Imported mix" in report.text
    assert "Created in YouTube Music" in report.text
    assert "Unmatched albums" in report.text and "Missing album" in report.text
    assert 'href="https://open.spotify.com/album/missingalbum"' in report.text
    assert 'href="https://open.spotify.com/track/missing"' in report.text
    assert "Retries do not create duplicates" in report.text

    dismissed = web.post("/import/spotify-library/dismiss")
    assert dismissed.status_code == 200
    saved = json.loads(store.get_setting("spotify_library_import_job"))
    assert saved["dismissed"] is True and saved["report"]["tracks_imported"] == 1
    assert "Spotify import complete" not in web.get("/").text
    assert web.get("/import/spotify-library/report").status_code == 200


def test_account_data_upload_returns_before_youtube_matching_finishes(store):
    identity = store.upsert_identity("main", "bridge", None, True)
    entered, release = threading.Event(), threading.Event()

    class SlowClient(FakeClient):
        def search(self, query, filter="songs"):
            entered.set()
            release.wait(2)
            return super().search(query, filter=filter)

    client = SlowClient(search_results=[_track("yt-song", "Song", "Artist")])
    web = TestClient(create_app(store, lambda: {identity: client}, now_fn=lambda: 1000.0),
                     base_url="http://127.0.0.1:8123")
    try:
        response = web.post("/import/spotify-library/v4", files={
            "file": ("spotify-account-data.zip", _library_payload(), "application/zip")})
        assert response.status_code == 200
        assert "running in the background" in response.text
        assert "could take about 5 minutes" in response.text
        assert entered.wait(1)
        assert json.loads(store.get_setting("spotify_library_import_job"))["status"] == "running"
    finally:
        release.set()
    assert _wait_for_library_job(store)["status"] == "done"


def test_technical_log_is_explained_as_the_wrong_spotify_package(store):
    identity = store.upsert_identity("main", "bridge", None, True)
    web = TestClient(create_app(store, lambda: {identity: FakeClient()}, now_fn=lambda: 1.0),
                     base_url="http://127.0.0.1:8123")
    wrong = io.BytesIO()
    with zipfile.ZipFile(wrong, "w") as archive:
        archive.writestr("Spotify Technical Log Information/Download.json", "[]")

    response = web.post("/import/spotify-library/v4", files={
        "file": ("technical.zip", wrong.getvalue(), "application/zip")})

    assert "Technical Log Information" in response.text
    assert "HX-Retarget" not in response.headers


def test_legacy_report_gets_spotify_search_links_and_can_resolve_a_candidate(store):
    identity = store.upsert_identity("main", "bridge", None, True)

    class AlbumClient(FakeClient):
        def __init__(self):
            super().__init__(albums={"MPRE_FAT": {
                "title": "The Fat of the Land - Expanded Edition",
                "artists": [{"name": "The Prodigy"}], "audioPlaylistId": "OLAK_FAT",
                "type": "Album", "year": "1997", "thumbnails": []}})
            self.album_ratings = []
            self.bridge_calls_on_event_loop = []

        def search(self, query, filter="songs"):
            try:
                asyncio.get_running_loop()
                self.bridge_calls_on_event_loop.append("search")
            except RuntimeError:
                pass
            return [{"browseId": "MPRE_FAT", "title": "The Fat of the Land - Expanded Edition",
                     "artists": [{"name": "The Prodigy"}], "thumbnails": []}]

        def get_album(self, browse_id):
            try:
                asyncio.get_running_loop()
                self.bridge_calls_on_event_loop.append("get_album")
            except RuntimeError:
                pass
            return super().get_album(browse_id)

        def rate_playlist(self, playlist_id, rating):
            self.album_ratings.append((playlist_id, rating))

    client = AlbumClient()
    web = TestClient(create_app(store, lambda: {identity: client}, now_fn=lambda: 2000.0),
                     base_url="http://127.0.0.1:8123")
    store.set_setting("spotify_library_import_job", json.dumps({
        "status": "done", "report_schema": 2, "playlists_found": 0, "track_entries": 0,
        "albums_found": 1, "report": {
            "playlists_found": 0, "playlists_imported": 0, "playlists_skipped": 0,
            "tracks_imported": 0, "tracks_unmatched": 0, "albums_found": 1,
            "albums_imported": 0, "albums_skipped": 0, "albums_unmatched": 1,
            "details": {"playlists": [], "unmatched_tracks": [], "albums": [{
                "title": "The Fat of the Land", "artist": "The Prodigy",
                "status": "unmatched", "reason": "no_confident_match"}]},
        },
    }))

    legacy = web.get("/import/spotify-library/report")
    assert "open.spotify.com/search/The%20Fat%20of%20the%20Land%20The%20Prodigy" in legacy.text
    assert "This older report did not retain its candidates" in legacy.text

    found = web.post("/import/spotify-library/find-album-candidates",
                     data={"album_index": "0"}, follow_redirects=False)
    assert found.status_code == 303
    with_candidates = web.get("/import/spotify-library/report")
    assert "The Fat of the Land - Expanded Edition" in with_candidates.text
    assert "Save this album" in with_candidates.text

    saved = web.post("/import/spotify-library/resolve-album",
                     data={"album_index": "0", "browse_id": "MPRE_FAT"},
                     follow_redirects=False)
    assert saved.status_code == 303
    job = json.loads(store.get_setting("spotify_library_import_job"))
    assert job["report"]["albums_unmatched"] == 0
    assert job["report"]["albums_imported"] == 1
    assert job["report"]["details"]["albums"][0]["reason"] == "saved_by_user"
    assert client.album_ratings == [("OLAK_FAT", "LIKE")]
    assert client.bridge_calls_on_event_loop == []
    assert store.get_saved_albums()[0]["browse"] == "MPRE_FAT"
