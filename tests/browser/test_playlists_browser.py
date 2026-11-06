"""Bulk selection, copy and deletion through the playlist library."""
import socket

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.conftest import FakeClient, _track
from tests.live_server import LiveServer
from yt_playlist.core import paths
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.browser


@pytest.fixture
def live_pl_app(tmp_path, monkeypatch, store):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    iid = store.accounts.upsert_account("Main", "cred", None, True)
    client_tracks = {}
    for suffix, title, song, artist in [("A", "Alpha", "SongA", "X"),
                                       ("B", "Beta", "SongB", "Y"),
                                       ("G", "Gamma", "SongC", "Z")]:
        video = f"v{len(client_tracks) + 1}"
        pid = store.playlists.upsert_playlist(iid, f"PL{suffix}", title, 1, "h", 1.0)
        store.playlists.set_playlist_tracks(pid, [store.tracks.upsert_track(video, song, artist, None, None, 1)])
        client_tracks[f"PL{suffix}"] = [_track(video, song, artist)]
    client = FakeClient(tracks=client_tracks)
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 2_000_000)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        with LiveServer(server, sockets=[sock]):
            yield f"http://127.0.0.1:{port}"


def _select(page, title):
    page.locator(".playlist-table-card").get_by_role("row").filter(has_text=title).get_by_role("checkbox").check()


def test_delete_removes_playlists_after_reload(live_pl_app, page):
    page.goto(f"{live_pl_app}/playlists")
    _select(page, "Beta")
    page.get_by_role("button", name="Delete", exact=True).click()
    page.get_by_role("button", name="Delete them").click()
    expect(page.get_by_role("link", name="Beta")).to_have_count(0)
    expect(page.get_by_role("link", name="Alpha")).to_be_visible()


def test_shift_click_selects_range(live_pl_app, page):
    page.goto(f"{live_pl_app}/playlists")
    _select(page, "Alpha")
    row = page.get_by_role("row").filter(has_text="Gamma")
    row.get_by_role("checkbox").click(modifiers=["Shift"])
    expect(page.get_by_text("3 selected")).to_be_visible()
    for title in ("Alpha", "Beta", "Gamma"):
        expect(page.get_by_role("row").filter(has_text=title).get_by_role("checkbox")).to_be_checked()


def test_copy_creates_new_playlist_after_reload(live_pl_app, page):
    page.goto(f"{live_pl_app}/playlists")
    _select(page, "Alpha")
    page.get_by_role("button", name="Copy", exact=True).click()
    inp = page.get_by_placeholder("New playlist name")
    inp.fill("Alpha Copy")
    inp.press("Enter")
    expect(page.locator(".playlist-table-card").get_by_role("link", name="Alpha Copy")).to_be_visible()
    expect(page.get_by_role("region", name="New playlists").get_by_role("link", name="Alpha Copy")).to_be_visible()
