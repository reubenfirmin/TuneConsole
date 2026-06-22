"""Live-behavior tests for the Save/Unsave album action on the artist page (/artist).

This is the albums-tab refactor target: the only fetch() in the albums/artist area is
the saveAlbum toggle, which lives on artist.html (the /albums page itself is pure client
sort). Saving must keep the collection table, the saved column, and the discography table
in sync, so the action does a full reload — converted to htmx HX-Refresh (parity).

Own fixture (shared live_app seeds discover data and is off-limits). Characterization
first: lock the CURRENT Alpine toggle, then keep it green after the htmx conversion.
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


class AlbumClient(FakeClient):
    """FakeClient that can resolve + rate an album (what save/unsave-album needs)."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.rated = []

    def get_album(self, browse_id):
        return {"audioPlaylistId": "PLAUDIO", "title": "The Album", "type": "Album",
                "artists": [{"name": "Artist X"}], "year": "2001", "thumbnails": []}

    def rate_playlist(self, playlist_id, rating):
        self.rated.append((playlist_id, rating))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_artist_app():
    s = Store(":memory:")
    s.init_schema()
    iid = s.upsert_identity("Main", "cred", None, True)
    pid = s.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    # a track with an album browse id -> the collection table renders a Save button for that album
    t = s.upsert_track("v1", "SongA", "Artist X", "The Album", 200, 1, album_browse_id="MPREb_x")
    s.set_playlist_tracks(pid, [t])
    client = AlbumClient()
    app = create_app(s, lambda: {iid: client}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield {"base": f"http://127.0.0.1:{port}", "store": s}
    server.should_exit = True
    thread.join(timeout=5)


def test_save_album_flips_to_unsave_after_reload(live_artist_app, page):
    page.goto(f"{live_artist_app['base']}/artist?name=Artist%20X")
    save = page.get_by_role("button", name="Save", exact=True)
    expect(save).to_be_visible()
    save.click()
    expect(page.get_by_role("button", name="Unsave", exact=True)).to_be_visible()   # now saved


def test_unsave_album_flips_to_save_after_reload(live_artist_app, page):
    # start from a saved album so the button reads "Unsave"
    live_artist_app["store"].add_saved_album({
        "browse": "MPREb_x", "title": "The Album", "artist": "Artist X",
        "year": "2001", "thumbnail": ""})
    page.goto(f"{live_artist_app['base']}/artist?name=Artist%20X")
    unsave = page.get_by_role("button", name="Unsave", exact=True)
    expect(unsave).to_be_visible()
    unsave.click()
    expect(page.get_by_role("button", name="Save", exact=True)).to_be_visible()      # now unsaved
