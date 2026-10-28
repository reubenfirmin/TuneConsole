import asyncio
import io
import json
import re
import threading
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeClient, _track
from yt_playlist.rec import mode_surfaces, rec_params, transient
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
    store.identities.upsert_identity("main", "bridge", None, True)
    store.tracks.upsert_track("yt", "Song", "Artist", None, 180)
    return TestClient(create_app(store, lambda: {}, now_fn=lambda: 1000.0),
                      base_url="http://127.0.0.1:8123")


def _wait_for_library_job(store, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = store.settings.get_setting("spotify_library_import_job")
        job = json.loads(raw) if raw else None
        if job and job["status"] not in ("queued", "running"):
            return job
        time.sleep(.01)
    raise AssertionError("Spotify library import did not finish")


def _select_library(web, payload=None):
    response = web.post("/import/spotify-library/v6", files={
        "file": ("spotify-account-data.zip", payload or _library_payload(), "application/zip")})
    assert response.status_code == 200
    return re.search(r'name="selection_token" value="([^"]+)"', response.text)[1], response


def _prepare_library(web, store, identity, *, payload=None, indices=(0,), albums=True):
    token, _ = _select_library(web, payload)
    response = web.post("/import/spotify-library/prepare", data={
        "selection_token": token, "identity_id": str(identity),
        "playlist_index": [str(index) for index in indices],
        "include_albums": "on" if albums else ""})
    assert response.status_code == 200
    job = _wait_for_library_job(store)
    assert job["status"] == "preview", job
    return job["preview_token"]


def test_setup_offers_local_export_upload_without_oauth(store):
    body = _client(store).get("/setup?tab=import").text
    assert "Import Spotify listening history" in body
    assert "Extended streaming history" in body
    assert "Account data" in body
    assert "playlists, saved songs, albums, and artists" in body
    assert "Import Spotify playlists and albums" in body
    assert "Be prepared to wait several days" in body
    assert "Playlist1.json" in body and "YourLibrary.json" in body
    assert 'hx-post="/import/spotify-library/v6"' in body
    assert "Restart TuneConsole" in body
    assert 'hx-post="/import/spotify/v2"' in body
    assert "Client ID" not in body and "/spotify/connect" not in body


def test_upload_imports_history_and_replaces_form_with_report(store):
    client = _client(store)
    response = client.post("/import/spotify/v2", files={
        "file": ("my_spotify_data.zip", _payload(), "application/zip")})
    assert response.status_code == 200
    assert "Spotify import complete" in response.text
    assert response.headers["HX-Retarget"] == "#spotify-import-block"
    assert store.settings.get_setting("spotify_imported_at") == "1000.0"


def test_history_upload_reaches_home_learning_and_requests_model_refresh(store, monkeypatch):
    now = 1704103200.0  # The export's timestamp, so its play participates in recent taste.
    store.identities.upsert_identity('main', 'bridge', None, True)
    tid = store.tracks.upsert_track('yt', 'Song', 'Artist', None, 180)
    store.tracks.set_track_genre(tid, 'Techno')
    rec_params.set_param(store, 'source_w_play', 1.0)
    rec_params.set_param(store, 'theme_threshold', 0.2)
    app = create_app(store, lambda: {}, now_fn=lambda: now, background_work=False)
    refreshes = []
    monkeypatch.setattr(app.state.ctx.rec_worker, 'trigger', lambda: refreshes.append(True))
    client = TestClient(app, base_url='http://127.0.0.1:8123')

    # File upload alone is sufficient; no additional permission or certification fields.
    response = client.post('/import/spotify/v2', files={
        'file': ('history.zip', _payload(), 'application/zip')})
    assert response.status_code == 200 and 'Spotify import complete' in response.text
    assert refreshes == [True]
    assert transient.play_facet_leans(store, now)
    assert client.get('/').status_code == 200
    assert store.rec_model.get_weights(now=now) == {}

    # Candidate pools precede Home in the worker; HTTP reads must not prepare either.
    assert mode_surfaces.prepare_bundles(store, now) is not None
    # Daily exposure belongs to background Home preparation, including after an import.
    worker = app.state.ctx.rec_worker
    worker.enabled = True
    worker.debounce_s = 0
    worker.trigger_home_refresh(force=True)
    worker.join(5)
    weights = store.rec_model.get_weights(now=now)
    assert weights and all(weight > 1.0 for weight in weights.values())

    client.post('/import/spotify/v2', files={'file': ('history.zip', _payload(), 'application/zip')})
    assert client.get('/').status_code == 200
    worker.trigger_home_refresh(force=True)
    worker.join(5)
    assert store.history.spotify_history_stats()['events'] == 1
    assert store.rec_model.get_weights(now=now) == weights
    assert refreshes == [True]

    removed = client.post('/import/spotify/remove')
    assert 'Saved taste preferences were kept' in removed.text
    assert refreshes == [True, True]
    assert transient.play_facet_leans(store, now) == {}
    assert store.rec_model.get_weights(now=now) == weights


def test_unreadable_upload_keeps_form_for_retry(store):
    response = _client(store).post("/import/spotify", files={
        "file": ("wrong.zip", b"not a spotify export", "application/zip")})
    assert response.status_code == 200
    assert "Could not find Spotify Extended Streaming History" in response.text
    assert "HX-Retarget" not in response.headers


def test_history_removal_is_available_after_upload_and_on_reload(store):
    client = _client(store)
    response = client.post('/import/spotify', files={'file':('history.zip',_payload(),'application/zip')})
    assert 'Remove Spotify history' in response.text
    assert 'Remove Spotify history' in client.get('/setup?tab=import').text
    iid = store.identities.get_identities()[0].id
    event = store.conn.execute('SELECT * FROM play_events').fetchone()
    store.history.import_listening_events(iid,[(event['identity_key'],None,event['played_at'])],source='takeout')
    response = client.post('/import/spotify/remove')
    assert response.status_code == 200
    assert response.headers['HX-Retarget'] == '#spotify-import-block'
    assert 'Spotify history removed' in response.text
    assert store.history.spotify_history_stats()['events'] == 0
    assert len(store.history.play_events_since(0)) == 1
    assert store.settings.get_setting('spotify_imported_at') is None
    assert 'Remove Spotify history' not in client.get('/setup?tab=import').text
    status = client.get('/import/history/status?source=spotify',headers={'accept':'application/json'}).json()
    assert status['done'] and status['outcome'] == 'complete'
    assert 'removed' in status['message']


def test_report_explains_quality_filters_without_exposing_private_fields(store):
    client = _client(store)
    rows = [dict(ts='2024-01-01T10:00:00Z',master_metadata_track_name='Song',
                 master_metadata_album_artist_name='Artist',spotify_track_uri='spotify:track:x',
                 ms_played=180000,ip_addr='PRIVATE_SENTINEL')]
    rows.extend([{**rows[0], 'ms_played':0},
                 {**rows[0], 'spotify_track_uri':'spotify:track:short','ms_played':1000},
                 {**rows[0], 'spotify_track_uri':'spotify:track:private','incognito_mode':True}])
    response = client.post('/import/spotify',files={'file':('audio.json',json.dumps(rows),'application/json')})
    assert '1 duplicate entries' in response.text
    assert '1 listens under 30 seconds' in response.text
    assert '1 private-session listens' in response.text
    assert 'PRIVATE_SENTINEL' not in response.text
    assert 'PRIVATE_SENTINEL' not in '\n'.join(store.conn.iterdump())


@pytest.mark.parametrize('source_timezone', ['UTC', 'America/Costa_Rica'])
def test_daily_only_spotify_history_can_be_removed(store, source_timezone):
    from yt_playlist.core import listening
    client = _client(store)
    iid = store.identities.get_identities()[0].id
    with store.conn:
        listening.calendar(store.conn, iid, 'UTC')
        listening.observe_day(store.conn, iid, 'old-song', '2024-01-01', 'spotify', source_timezone, 100)
    assert store.history.spotify_history_stats()['events'] == 0
    assert store.history.spotify_history_stats()['days'] == int(source_timezone == 'UTC')
    assert store.history.spotify_history_stats()['unresolved_days'] == int(source_timezone != 'UTC')
    assert 'Remove Spotify history' in client.get('/setup?tab=import').text
    assert client.post('/import/spotify/remove').status_code == 200
    assert store.history.spotify_history_stats()['days'] == 0
    assert store.history.spotify_history_stats()['unresolved_days'] == 0
    assert 'Remove Spotify history' not in client.get('/setup?tab=import').text


def test_account_data_requires_selection_preview_and_confirmation_before_writes(store):
    identity = store.identities.upsert_identity("main", "bridge", None, True)
    client = FakeClient(search_results=[_track("yt-song", "Song", "Artist")],
                        catalog={"yt-song": _track("yt-song", "Song", "Artist")})
    web = TestClient(create_app(store, lambda: {identity: client}, now_fn=lambda: 1000.0),
                     base_url="http://127.0.0.1:8123")

    token, response = _select_library(web)
    assert response.headers["HX-Retarget"] == "#spotify-library-import-block"
    assert "Choose what to import" in response.text
    assert "YouTube Music destination" in response.text
    assert client.created == []
    assert store.settings.get_setting("spotify_library_import_job") is None
    prepared = web.post("/import/spotify-library/prepare", data={
        "selection_token": token, "identity_id": str(identity), "playlist_index": "0",
        "include_albums": "on"})
    assert prepared.status_code == 200
    ready = _wait_for_library_job(store)
    assert ready["status"] == "preview"
    assert client.created == [] and store.playlists.get_playlists() == []
    preview = web.get(f"/import/spotify-library/preview/{ready['preview_token']}")
    assert "Confident match" in preview.text and "Unmatched" in preview.text
    assert "Missing song" in preview.text and "Source: Spotify" in preview.text
    confirmed = web.post("/import/spotify-library/confirm", data={"preview_token": ready["preview_token"]},
                         follow_redirects=False)
    assert confirmed.status_code == 303

    job = _wait_for_library_job(store)
    assert job["status"] == "done"
    assert job["report_schema"] == 6
    assert {k: v for k, v in job["report"].items() if k != "details"} == {
        "playlists_found": 1, "playlists_imported": 1, "playlists_skipped": 0, "playlists_failed": 0,
        "tracks_imported": 1, "tracks_unmatched": 1,
        "albums_found": 1, "albums_imported": 0, "albums_skipped": 0,
        "albums_unmatched": 1, "albums_as_playlists": 0,
    }
    assert job["report"]["details"]["playlists"][0]["name"] == "Imported mix"
    assert job["report"]["details"]["playlists"][0]["status"] == "imported"
    assert client.created[0][1] == "Imported mix"
    assert store.settings.get_setting("spotify_library_imported_at") == "1000.0"

    status = web.get("/import/spotify-library/status", headers={"HX-Request": "true"})
    assert status.headers["HX-Redirect"] == "/#notices"
    assert "View latest Spotify import report" in status.text

    import_page = web.get("/setup?tab=import")
    assert import_page.text.count("View latest Spotify import report") == 1
    assert "The report is ready with everything added" not in import_page.text

    assert "Spotify import complete" in web.get("/").text
    store.settings.set_setting("last_sync_at", "999.0")
    dashboard = web.get("/")
    assert "Spotify import complete" in dashboard.text
    assert "Review what was added, skipped, and unmatched" in dashboard.text
    assert 'href="/import/spotify-library/report"' in dashboard.text
    assert "Created in YouTube Music" not in dashboard.text
    # Setup keeps the completed report visible without opening the mature dashboard.
    assert 'id="onboarding-feed"' in dashboard.text
    assert 'class="dash-nav"' not in dashboard.text
    from tests.onboarding_support import publish_personal_mix
    web.post('/home/onboard/format', data={'format': 'mixes'})
    publish_personal_mix(store, now=1000)
    dashboard = web.get("/")
    assert "Spotify import complete" in dashboard.text
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
    assert job["report"]["details"]["unmatched_tracks"] == []
    assert "Missing song" not in store.settings.get_setting("spotify_library_import_job")
    assert "Retries do not create duplicates" in report.text

    dismissed = web.post("/import/spotify-library/dismiss")
    assert dismissed.status_code == 200
    saved = json.loads(store.settings.get_setting("spotify_library_import_job"))
    assert saved["dismissed"] is True and saved["report"]["tracks_imported"] == 1
    assert "Spotify import complete" not in web.get("/").text
    assert web.get("/import/spotify-library/report").status_code == 200


def test_account_data_upload_returns_before_youtube_matching_finishes(store):
    identity = store.identities.upsert_identity("main", "bridge", None, True)
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
        token, _ = _select_library(web)
        response = web.post("/import/spotify-library/prepare", data={
            "selection_token": token, "identity_id": str(identity), "playlist_index": "0"})
        assert response.status_code == 200
        assert "running in the background" in response.text
        assert "could take about 5 minutes" in response.text
        assert entered.wait(1)
        assert json.loads(store.settings.get_setting("spotify_library_import_job"))["status"] == "running"
    finally:
        release.set()
    assert _wait_for_library_job(store)["status"] == "preview"
    assert client.created == []


def test_technical_log_is_explained_as_the_wrong_spotify_package(store):
    identity = store.identities.upsert_identity("main", "bridge", None, True)
    web = TestClient(create_app(store, lambda: {identity: FakeClient()}, now_fn=lambda: 1.0),
                     base_url="http://127.0.0.1:8123")
    wrong = io.BytesIO()
    with zipfile.ZipFile(wrong, "w") as archive:
        archive.writestr("Spotify Technical Log Information/Download.json", "[]")

    response = web.post("/import/spotify-library/v4", files={
        "file": ("technical.zip", wrong.getvalue(), "application/zip")})

    assert "Technical Log Information" in response.text
    assert "HX-Retarget" not in response.headers


def test_playlist_selection_and_preview_bind_the_destination_and_recording(store):
    first = store.identities.upsert_identity("first", "first-bridge", None, True)
    second = store.identities.upsert_identity("second", "second-bridge", None, False)
    first_client = FakeClient(search_results=[_track("wrong-account", "Song", "Artist")])
    second_client = FakeClient(search_results=[_track("chosen-recording", "Song", "Artist")])
    web = TestClient(create_app(store, lambda: {first: first_client, second: second_client}, now_fn=lambda: 1000),
                     base_url="http://127.0.0.1:8123")
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("Playlist1.json", json.dumps({"playlists": [
            {"name": name, "items": [{"track": {"trackName": "Song", "artistName": "Artist"}}]}
            for name in ("Excluded", "Selected")] }))
    token = _prepare_library(web, store, second, payload=payload.getvalue(), indices=(1,), albums=False)
    preview = web.get(f"/import/spotify-library/preview/{token}")
    assert "Selected" in preview.text and "Excluded" not in preview.text
    assert "Destination: <strong>second</strong>" in preview.text
    assert "chosen-recording" in preview.text
    second_client._search_results = [_track("changed-after-preview", "Song", "Artist")]
    response = web.post("/import/spotify-library/confirm", data={
        "preview_token": token, "identity_id": str(first)}, follow_redirects=False)
    assert response.status_code == 303
    assert _wait_for_library_job(store)["status"] == "done"
    assert first_client.created == []
    assert second_client.added == [(second_client.created[0][0], ["chosen-recording"])]
    assert store.playlists.get_playlists()[0].identity_id == second
    replay = web.post("/import/spotify-library/confirm", data={"preview_token": token})
    assert replay.status_code == 410
    assert len(second_client.created) == 1


def test_expired_and_cancelled_previews_cannot_write(store):
    identity = store.identities.upsert_identity("main", "bridge", None, True)
    client = FakeClient(search_results=[_track("yt", "Song", "Artist")])
    clock = [1000]
    web = TestClient(create_app(store, lambda: {identity: client}, now_fn=lambda: clock[0]),
                     base_url="http://127.0.0.1:8123")
    token = _prepare_library(web, store, identity, albums=False)
    clock[0] += 7201
    assert web.get(f"/import/spotify-library/preview/{token}").status_code == 410
    assert web.post("/import/spotify-library/confirm", data={"preview_token": token}).status_code == 410
    token = _prepare_library(web, store, identity, albums=False)
    response = web.post("/import/spotify-library/cancel", data={"preview_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert web.post("/import/spotify-library/confirm", data={"preview_token": token}).status_code == 410
    assert client.created == [] and store.playlists.get_playlists() == []


def test_invalid_selection_and_disconnected_destination_do_not_consume_upload(store):
    identity = store.identities.upsert_identity("main", "bridge", None, True)
    client = FakeClient(search_results=[_track("yt", "Song", "Artist")])
    web = TestClient(create_app(store, lambda: {identity: client}, now_fn=lambda: 1000),
                     base_url="http://127.0.0.1:8123")
    token, _ = _select_library(web)
    base = {"selection_token": token, "identity_id": str(identity)}
    for index in ("-1", "1", "garbage"):
        assert web.post("/import/spotify-library/prepare", data={**base, "playlist_index": index}).status_code == 400
    assert web.post("/import/spotify-library/prepare", data=base).status_code == 400
    assert web.post("/import/spotify-library/prepare", data={
        **base, "identity_id": "999", "playlist_index": "0"}).status_code == 409
    assert web.post("/import/spotify-library/prepare", data={**base, "playlist_index": "0"}).status_code == 200
    assert _wait_for_library_job(store)["status"] == "preview"
    assert client.created == []


def test_failed_playlist_result_remains_reviewable_with_successes(store):
    class BrokenFirst(FakeClient):
        def create_playlist(self, title, description):
            if title == "Broken":
                raise RuntimeError("create rejected")
            return super().create_playlist(title, description)

    identity = store.identities.upsert_identity("main", "bridge", None, True)
    client = BrokenFirst(search_results=[_track("yt", "Song", "Artist")],
                         catalog={"yt": _track("yt", "Song", "Artist")})
    web = TestClient(create_app(store, lambda: {identity: client}, now_fn=lambda: 1000),
                     base_url="http://127.0.0.1:8123")
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("Playlist1.json", json.dumps({"playlists": [
            {"name": name, "items": [{"track": {"trackName": "Song", "artistName": "Artist"}}]}
            for name in ("Broken", "Good")] }))
    token = _prepare_library(web, store, identity, payload=payload.getvalue(), indices=(0, 1), albums=False)
    web.post("/import/spotify-library/confirm", data={"preview_token": token}, follow_redirects=False)
    job = _wait_for_library_job(store)
    assert job["status"] == "done"
    assert job["report"]["playlists_failed"] == job["report"]["playlists_imported"] == 1
    report = web.get("/import/spotify-library/report")
    assert report.status_code == 200
    assert "playlist transfers need a retry" in report.text
    assert "Broken" in report.text and "Good" in report.text
    assert client.created[0][1] == "Good"


def test_legacy_report_gets_spotify_search_links_and_can_resolve_a_candidate(store):
    identity = store.identities.upsert_identity("main", "bridge", None, True)

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
    store.settings.set_setting("spotify_library_import_job", json.dumps({
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
    job = json.loads(store.settings.get_setting("spotify_library_import_job"))
    assert job["report"]["albums_unmatched"] == 0
    assert job["report"]["albums_imported"] == 1
    assert job["report"]["details"]["albums"][0]["reason"] == "saved_by_user"
    assert client.album_ratings == [("OLAK_FAT", "LIKE")]
    assert client.bridge_calls_on_event_loop == []
    assert store.collection.get_saved_albums()[0]["browse"] == "MPRE_FAT"
