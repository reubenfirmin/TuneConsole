"""ZIP selection, HTMX preview and confirmed import with controlled providers."""
from contextlib import contextmanager
import io
import json
import socket
import time
import zipfile
from types import SimpleNamespace

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.conftest import FakeClient, _track
from tests.live_server import LiveServer
from tests.web.test_spotify_data_route import _wait_for_library_job
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.browser


@pytest.fixture
def spotify_app(store, record_property):
    @contextmanager
    def phase(name):
        began = time.perf_counter()
        try:
            yield
        finally:
            record_property('spotify_browser_' + name + '_seconds', time.perf_counter() - began)

    began = time.perf_counter()
    first = store.accounts.upsert_account("First account", "first", None, True)
    second = store.accounts.upsert_account("Destination account", "second", None, False)
    first_song = _track("first-song", "Song", "Artist")
    chosen_song = _track("chosen-song", "Song", "Artist")
    first_client = FakeClient(search_results=[first_song], catalog={"first-song": first_song})
    second_client = FakeClient(search_results=[chosen_song], catalog={"chosen-song": chosen_song})
    app = create_app(store, lambda: {first: first_client, second: second_client}, now_fn=lambda: 1000)
    record_property('spotify_browser_app_construction_seconds', time.perf_counter() - began)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        began = time.perf_counter()
        with LiveServer(server, sockets=[sock]):
            record_property('spotify_browser_server_startup_seconds', time.perf_counter() - began)
            yield SimpleNamespace(base=f"http://127.0.0.1:{port}", store=store,
                                  first=first_client, second=second_client, destination=second,
                                  worker=app.state.ctx.rec_worker, app=app, phase=phase)


def complete_spotify_job(spotify_app, page, *, status, poll_path, poll_ms):
    with spotify_app.phase(status + '_backend_completion'):
        job = _wait_for_library_job(spotify_app, timeout=15)
        assert job['status'] == status, job
    with spotify_app.phase(status + '_browser_poll'):
        if page.locator(f'[hx-get="{poll_path}"]').count():
            with page.expect_response(lambda response: response.request.method == 'GET'
                    and response.url.split('?')[0].endswith(poll_path)) as response:
                page.clock.fast_forward(poll_ms)
            assert response.value.status == 200
    return job


def test_zip_import_waits_for_confirmation_and_uses_selected_destination(spotify_app, page):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("Playlist1.json", json.dumps({"playlists": [
            {"name": name, "items": [{"track": {"trackName": "Song", "artistName": "Artist"}}]}
            for name in ("Selected mix", "Excluded mix")]}))
    page.set_viewport_size({"width": 1280, "height": 900})
    page.clock.install()
    with spotify_app.phase('setup_navigation'):
        page.goto(f"{spotify_app.base}/setup?tab=import")
    page.locator('input[type="file"][accept=".zip,application/zip"]').set_input_files({
        "name": "spotify-account-data.zip", "mimeType": "application/zip", "buffer": payload.getvalue()})
    page.get_by_role("button", name="Choose playlists and destination").click()
    expect(page.get_by_role("heading", name="Choose what to import")).to_be_visible()
    page.get_by_label("YouTube Music destination").select_option(str(spotify_app.destination))
    page.get_by_label("Excluded mix", exact=False).uncheck()
    page.get_by_role("button", name="Prepare preview").click()
    page.wait_for_function("""() => location.pathname.includes('/preview/') ||
        !!document.querySelector(
            'a[href*="/import/spotify-library/preview/"], [hx-get="/import/spotify-library/status"]')""")
    complete_spotify_job(spotify_app, page, status='preview',
                         poll_path='/import/spotify-library/status', poll_ms=5000)
    page.wait_for_function("""() => location.pathname.includes('/preview/') ||
        !!document.querySelector('a[href*="/import/spotify-library/preview/"]')""")
    if "/preview/" not in page.url:
        page.get_by_role("link", name="Review matches and confirm").click()
    expect(page.get_by_role("heading", name="Preview Spotify import")).to_be_visible()
    expect(page.get_by_role("heading", name="Selected mix")).to_be_visible()
    expect(page.get_by_role("heading", name="Excluded mix")).to_have_count(0)
    expect(page.get_by_text("Confident match", exact=True)).to_be_visible()
    assert spotify_app.first.created == spotify_app.second.created == []
    assert spotify_app.first.added == spotify_app.second.added == []
    with spotify_app.phase('preview_screenshot'):
        page.screenshot(path="/tmp/tuneconsole-spotify-preview.png", full_page=True, animations="disabled")
    with spotify_app.phase('confirm_navigation'):
        page.get_by_role("button", name="Confirm import to Destination account").click()
    expect(page.locator('#spotify-library-notice')).to_be_visible()
    job = complete_spotify_job(spotify_app, page, status='done',
                              poll_path='/import/spotify-library/notice', poll_ms=10000)
    expect(page.get_by_text("Spotify import complete.", exact=True)).to_be_visible(timeout=15000)
    assert spotify_app.first.created == []
    assert spotify_app.second.added == [(spotify_app.second.created[0][0], ["chosen-song"])]
    assert spotify_app.store.playlists.get_playlists()[0].account_id == spotify_app.destination
    with spotify_app.phase('confirmed_readback'):
        assert job['report']['playlists_imported'] == job['report']['tracks_imported'] == 1
        assert job['report']['playlists_failed'] == 0
        playlists = spotify_app.store.playlists.get_playlists()
        assert len(playlists) == len(spotify_app.second.created) == 1
        remote = spotify_app.second.created[0][0]
        assert playlists[0].provider_playlist_id == remote
        assert [track['videoId'] for track in spotify_app.second.get_playlist(
            remote, limit=None)['tracks']] == ['chosen-song']
        assert [row[1] for row in spotify_app.store.playlists.get_playlist_tracks_with_meta(
            playlists[0].id)] == ['chosen-song']
