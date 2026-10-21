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
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.browser


@pytest.fixture
def spotify_app(store):
    first = store.upsert_identity("First account", "first", None, True)
    second = store.upsert_identity("Destination account", "second", None, False)
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
    assert spotify_app.store.get_playlists()[0].identity_id == spotify_app.destination
