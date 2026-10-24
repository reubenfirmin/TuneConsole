"""Album song titles open track details; play controls route through /play to the deck tab.

Needs a real browser: the behavior under test is playback-controls.js's capture-phase click interceptor, which
TestClient cannot run.
"""
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect

from yt_playlist.core.store import Store
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
def live_album_app():
    s = Store(":memory:")
    s.init_schema()
    iid = s.identities.upsert_identity("Main", "cred", None, True)
    album = {"title": "The Album", "year": "2001", "artists": [{"name": "Artist X"}], "thumbnails": [],
             "tracks": [{"title": "One", "videoId": "v1", "duration": "3:01",
                         "artists": [{"name": "Artist X"}]}]}
    app = create_app(s, lambda: {iid: FakeClient(albums={"MPREb_x": album})}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _wire(page, plays):
    """Never let a test reach YouTube for real, and record any /play the interceptor fires."""
    page.context.route("**://music.youtube.com/**",
                       lambda route: route.fulfill(status=200, content_type="text/html", body="ytm"))
    page.context.route("**/play", lambda route: (plays.append(route.request.post_data),
                                                 route.fulfill(json={"ok": True})))


def test_album_song_title_opens_details_without_changing_playback(live_album_app, page):
    plays = []
    _wire(page, plays)
    page.goto(f"{live_album_app}/album?browse=MPREb_x")
    expect(page.locator("a.ptitle")).to_have_count(1)
    assert page.locator("a.ptitle").get_attribute("href").startswith("/track/v1?")
    page.locator("a.ptitle").click()
    expect(page.get_by_role("heading", name="One", exact=True)).to_be_visible()
    assert page.url.startswith(f"{live_album_app}/track/v1?")
    assert len(page.context.pages) == 1
    assert plays == []


def test_play_button_still_routes_to_the_existing_tab(live_album_app, page):
    plays = []
    _wire(page, plays)
    page.goto(f"{live_album_app}/album?browse=MPREb_x")
    page.click("a.pl-play")
    page.wait_for_timeout(300)
    assert len(plays) == 1                         # still a play open: same tab, via /play
    assert "v1" in plays[0]
