import io
import json
import zipfile

from fastapi.testclient import TestClient

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


def _client(store):
    store.upsert_identity("main", "bridge", None, True)
    store.upsert_track("yt", "Song", "Artist", None, 180)
    return TestClient(create_app(store, lambda: {}, now_fn=lambda: 1000.0),
                      base_url="http://127.0.0.1:8123")


def test_setup_offers_local_export_upload_without_oauth(store):
    body = _client(store).get("/setup?tab=import").text
    assert "Import Spotify listening history" in body
    assert "Extended streaming history" in body
    assert "Account data" in body
    assert "playlists, saved songs, albums, and artists" in body
    assert "Account data zip is coming next" in body
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
