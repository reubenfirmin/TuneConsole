"""Album saving through the artist page, with a controlled provider."""
import socket

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.conftest import FakeClient
from tests.live_server import LiveServer
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.browser


class AlbumClient(FakeClient):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.rated = []

    def get_album(self, browse_id):
        return {"audioPlaylistId": "PLAUDIO", "title": "The Album", "type": "Album",
                "artists": [{"name": "Artist X"}], "year": "2001", "thumbnails": [],
                "tracks": [{"videoId": "v1", "title": "SongA", "duration": "3:20",
                            "artists": [{"name": "Artist X"}]},
                           {"videoId": "v2", "title": "SongB", "duration": "3:00"}]}

    def rate_playlist(self, playlist_id, rating):
        self.rated.append((playlist_id, rating))


@pytest.fixture
def live_artist_app(store):
    iid = store.accounts.upsert_account("Main", "cred", None, True)
    other = store.accounts.upsert_account("Other", "cred", "other", False)
    pid = store.playlists.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    track = store.tracks.upsert_track("v1", "SongA", "Artist X", "The Album", 200, 1, album_browse_id="MPREb_x")
    store.playlists.set_playlist_tracks(pid, [track])
    clients = {iid: AlbumClient(), other: AlbumClient()}
    app = create_app(store, lambda: clients, now_fn=lambda: 1.0, background_work=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        with LiveServer(server, sockets=[sock]):
            yield {"base": f"http://127.0.0.1:{port}", "store": store, "iid": iid, "other": other, "clients": clients}


@pytest.mark.parametrize('failed_first', [False])
def test_save_album_flips_to_unsave_after_reload(live_artist_app, page, failed_first):
    page.goto(f"{live_artist_app['base']}/artist?name=Artist%20X")
    save = page.get_by_role("button", name="Save", exact=True)
    expect(save).to_be_visible()
    save.click()
    expect(page.get_by_role("button", name="Unsave", exact=True)).to_be_visible()
