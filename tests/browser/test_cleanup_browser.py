"""Resolve an exact duplicate playlist group through Cleanup."""
import socket

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.conftest import FakeClient
from tests.live_server import LiveServer
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.browser


@pytest.fixture
def live_cleanup_app(store):
    iid = store.accounts.upsert_account("Main", "c1", None, True)
    tracks = [store.tracks.upsert_track(f"d{i}", f"D{i}", "X", None, None, 1) for i in range(2)]
    for ytm, title in (("PLD1", "Dup A"), ("PLD2", "Dup B")):
        pid = store.playlists.upsert_playlist(iid, ytm, title, 2, "h", 1.0)
        store.playlists.set_playlist_tracks(pid, tracks)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        with LiveServer(server, sockets=[sock]):
            yield f"http://127.0.0.1:{port}"


def test_keep_one_resolves_duplicate_group(live_cleanup_app, page):
    page.goto(f"{live_cleanup_app}/cleanup")
    expect(page.get_by_text("identical copies")).to_be_visible()
    page.get_by_role("button", name="Keep this one").first.click()
    page.get_by_text("identical copies").wait_for(state="hidden", timeout=4000)
