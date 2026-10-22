"""Exercise ZIP selection, HTMX preview, and confirmation with fake YouTube clients."""
import io
import json
import socket
import threading
import time
import zipfile
from types import SimpleNamespace

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.conftest import FakeClient, _track
from yt_playlist.rec import rec_params
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.browser


@pytest.fixture
def spotify_app(store):
    first = store.identities.upsert_identity("First account", "first", None, True)
    second = store.identities.upsert_identity("Destination account", "second", None, False)
    first_client = FakeClient(search_results=[_track("first-song", "Song", "Artist")])
    second_client = FakeClient(search_results=[_track("chosen-song", "Song", "Artist")])
    app = create_app(store, lambda: {first: first_client, second: second_client}, now_fn=lambda: 1000)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "test server did not start"
        time.sleep(.02)
    try:
        yield SimpleNamespace(base=f"http://127.0.0.1:{port}", store=store,
                              first=first_client, second=second_client, destination=second)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_zip_import_waits_for_confirmation_and_uses_selected_destination(spotify_app, page):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("Playlist1.json", json.dumps({"playlists": [
            {"name": name, "items": [{"track": {"trackName": "Song", "artistName": "Artist"}}]}
            for name in ("Selected mix", "Excluded mix")]}))
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{spotify_app.base}/setup?tab=import")
    page.locator('input[type="file"][accept=".zip,application/zip"]').set_input_files({
        "name": "spotify-account-data.zip", "mimeType": "application/zip", "buffer": payload.getvalue()})
    page.get_by_role("button", name="Choose playlists and destination").click()
    expect(page.get_by_role("heading", name="Choose what to import")).to_be_visible()
    page.get_by_label("YouTube Music destination").select_option(str(spotify_app.destination))
    page.get_by_label("Excluded mix", exact=False).uncheck()
    page.get_by_role("button", name="Prepare preview").click()
    page.wait_for_function("""() => location.pathname.includes('/preview/') ||
        !!document.querySelector('a[href*="/import/spotify-library/preview/"]')""")
    if "/preview/" not in page.url:
        page.get_by_role("link", name="Review matches and confirm").click()
    expect(page.get_by_role("heading", name="Preview Spotify import")).to_be_visible()
    expect(page.get_by_role("heading", name="Selected mix")).to_be_visible()
    expect(page.get_by_role("heading", name="Excluded mix")).to_have_count(0)
    expect(page.get_by_text("Confident match", exact=True)).to_be_visible()
    assert spotify_app.first.created == spotify_app.second.created == []
    page.screenshot(path="/tmp/tuneconsole-spotify-preview.png", full_page=True, animations="disabled")
    page.get_by_role("button", name="Confirm import to Destination account").click()
    expect(page.get_by_text("Spotify import complete.", exact=True)).to_be_visible(timeout=15000)
    assert spotify_app.first.created == []
    assert spotify_app.second.added == [(spotify_app.second.created[0][0], ["chosen-song"])]
    assert spotify_app.store.playlists.get_playlists()[0].identity_id == spotify_app.destination


def test_history_upload_health_learning_and_removal(spotify_app, page):
    store = spotify_app.store
    tid = store.tracks.upsert_track('history-song', 'Song', 'Artist', None, 180)
    store.tracks.set_track_genre(tid, 'Techno')
    key = store.tracks.identity_key_for_video('history-song')
    iid = store.identities.get_identities()[0].id
    store.history.import_listening_events(iid, [(key, None, 1000)], source='takeout')
    rec_params.set_param(store, 'source_w_play', 1.0)
    rec_params.set_param(store, 'theme_threshold', 0.2)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, 'w') as archive:
        archive.writestr('Streaming_History_Audio_1970.json', json.dumps([{
            'ts': '1970-01-01T00:16:40Z', 'master_metadata_track_name': 'Song',
            'master_metadata_album_artist_name': 'Artist', 'spotify_track_uri': 'spotify:track:one',
            'ms_played': 180000}]))

    page.goto(f'{spotify_app.base}/setup?tab=import')
    form = page.locator('form[hx-post="/import/spotify/v2"]')
    form.locator('input[type="file"]').set_input_files({
        'name': 'spotify-history.zip', 'mimeType': 'application/zip', 'buffer': payload.getvalue()})
    form.get_by_role('button', name='Import Spotify history', exact=True).click()
    expect(page.get_by_text('Spotify import complete.', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='Remove Spotify history', exact=True)).to_be_visible()
    assert store.history.spotify_history_stats()['events'] == 1
    assert len(store.history.play_events_since(0)) == 1

    page.goto(f'{spotify_app.base}/')
    weights = store.rec_model.get_weights(now=1000)
    assert weights and all(weight > 1.0 for weight in weights.values())
    page.goto(f'{spotify_app.base}/taste#health')
    health = page.locator('#taste-health-panel')
    expect(health.get_by_role('heading', name='Spotify listening history')).to_be_visible()
    expect(health.get_by_text('1 timestamped plays across 1 library tracks;', exact=False)).to_be_visible()
    expect(health.get_by_text('gradually update saved taste preferences.', exact=False)).to_be_visible()
    health.get_by_role('link', name='Manage or remove Spotify history').click()
    expect(page.get_by_role('button', name='Remove Spotify history', exact=True)).to_be_visible()
    page.reload()
    page.get_by_role('button', name='Remove Spotify history', exact=True).click()
    expect(page.get_by_text('Spotify history removed.', exact=True)).to_be_visible()
    expect(page.get_by_text('Saved taste preferences were kept.', exact=True)).to_be_visible()
    assert store.history.spotify_history_stats()['events'] == 0
    assert len(store.history.play_events_since(0)) == 1
    assert store.history.recent_plays_with_ts() == [(key, 1000.0)]
    page.reload()
    expect(page.get_by_role('button', name='Remove Spotify history', exact=True)).to_have_count(0)
