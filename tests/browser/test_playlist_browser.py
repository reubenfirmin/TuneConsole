"""Persist a playlist rename through the rendered detail page."""
import socket

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.conftest import FakeClient
from tests.live_server import LiveServer
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.browser


@pytest.fixture
def live_playlist_app(store):
    iid = store.accounts.upsert_account("Main", "cred", None, True)
    pid = store.playlists.upsert_playlist(iid, "PL1", "Mix", 2, "h", 1.0)
    tracks = [store.tracks.upsert_track(f"v{i}", title, artist, "Alb", 200, 1)
              for i, title, artist in [(0, "Song A", "Artist X"), (1, "Song B", "Artist Y")]]
    store.playlists.set_playlist_tracks(pid, tracks)
    client = FakeClient(tracks={"PL1": [{"videoId": f"v{i}", "setVideoId": f"sv{i}"} for i in range(2)]})
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 1.0)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        with LiveServer(server, sockets=[sock]):
            yield {"base": f"http://127.0.0.1:{port}", "pid": pid, "store": store, "client": client}


@pytest.mark.parametrize('failed_first', [False])
def test_rename_playlist_updates_heading(live_playlist_app, page, failed_first):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.goto(f"{base}/playlist/{pid}")
    page.get_by_role("heading", name="Mix").click()
    inp = page.locator("input.title-input")
    inp.fill("Renamed Mix")
    inp.press("Enter")
    expect(page.get_by_role("heading", name="Renamed Mix")).to_be_visible()
    page.goto(f"{base}/playlist/{pid}")
    expect(page.get_by_role("heading", name="Renamed Mix")).to_be_visible()
