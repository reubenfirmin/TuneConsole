"""Live-behavior tests for the playlist detail page (/playlist/{id}).

Own fixture (a playlist with a couple of tracks; the shared live_app seeds discover data
and is off-limits). Characterization first: lock the CURRENT Alpine behavior, then keep it
green after each Alpine->htmx conversion (rename, year/genre cells, enrich, remove, reorder,
alternates). Asserts on user-visible outcomes, not the transport.
"""
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect

from yt_playlist.store import Store
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient

pytestmark = pytest.mark.browser


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_playlist_app():
    s = Store(":memory:")
    s.init_schema()
    iid = s.upsert_identity("Main", "cred", None, True)
    pid = s.upsert_playlist(iid, "PL1", "Mix", 2, "h", 1.0)
    t0 = s.upsert_track("v0", "Song A", "Artist X", "Alb", 200, 1)
    t1 = s.upsert_track("v1", "Song B", "Artist Y", "Alb", 200, 1)
    s.set_playlist_tracks(pid, [t0, t1])
    s.set_track_genre(t0, "Rock")            # one track starts with a genre, one blank
    client = FakeClient(tracks={"PL1": [{"videoId": "v0", "setVideoId": "sv0"},
                                        {"videoId": "v1", "setVideoId": "sv1"}]})
    app = create_app(s, lambda: {iid: client}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield {"base": f"http://127.0.0.1:{port}", "pid": pid, "store": s}
    server.should_exit = True
    thread.join(timeout=5)


def test_rename_playlist_updates_heading(live_playlist_app, page):
    base, pid = live_playlist_app["base"], live_playlist_app["pid"]
    page.goto(f"{base}/playlist/{pid}")
    page.get_by_role("heading", name="Mix").click()        # click-to-edit
    inp = page.locator("input.title-input")
    inp.fill("Renamed Mix")
    inp.press("Enter")
    expect(page.get_by_role("heading", name="Renamed Mix")).to_be_visible()
    # survives a reload (persisted, not just a DOM poke)
    page.goto(f"{base}/playlist/{pid}")
    expect(page.get_by_role("heading", name="Renamed Mix")).to_be_visible()
