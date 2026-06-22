"""Live-behavior tests for the Playlists home page (/) bulk actions.

Own fixture (one identity + a couple of seeded playlists with tracks), since the
shared live_app seeds discover data and is off-limits. Characterization first:
these lock the CURRENT Alpine behavior (select -> modal -> confirm -> reload) and
must keep passing after the bulk actions move to htmx. The client-side list (sort,
multi-select, split, prefs) is unchanged Alpine and is exercised incidentally here.
"""
import os
import re
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect

from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from tests.conftest import FakeClient, _track

pytestmark = pytest.mark.browser


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_pl_app(tmp_path):
    # delete backs up to disk first, so point YT_PLAYLIST_HOME at a temp dir for this server.
    old_home = os.environ.get("YT_PLAYLIST_HOME")
    os.environ["YT_PLAYLIST_HOME"] = str(tmp_path)

    s = Store(":memory:")
    s.init_schema()
    iid = s.upsert_identity("Main", "cred", None, True)
    a = s.upsert_playlist(iid, "PLA", "Alpha", 1, "h", 1.0)
    b = s.upsert_playlist(iid, "PLB", "Beta", 1, "h", 1.0)
    s.set_playlist_tracks(a, [s.upsert_track("v1", "SongA", "X", None, None, 1)])
    s.set_playlist_tracks(b, [s.upsert_track("v2", "SongB", "Y", None, None, 1)])
    client = FakeClient(tracks={"PLA": [_track("v1", "SongA", "X")],
                                "PLB": [_track("v2", "SongB", "Y")]})
    app = create_app(s, lambda: {iid: client}, now_fn=lambda: 1.0)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
    if old_home is None:
        os.environ.pop("YT_PLAYLIST_HOME", None)
    else:
        os.environ["YT_PLAYLIST_HOME"] = old_home


def _select(page, title):
    page.get_by_role("row").filter(has_text=title).get_by_role("checkbox").check()


def test_group_assigns_group_after_reload(live_pl_app, page):
    page.goto(f"{live_pl_app}/playlists")
    _select(page, "Alpha")
    _select(page, "Beta")
    page.get_by_role("button", name=re.compile("Group")).click()
    inp = page.get_by_placeholder("e.g. Workout")
    inp.fill("Faves")
    inp.press("Enter")                                  # confirm -> server -> full reload
    expect(page.get_by_text("Faves").first).to_be_visible()   # group tag rendered after reload


def test_delete_removes_playlists_after_reload(live_pl_app, page):
    page.goto(f"{live_pl_app}/playlists")
    _select(page, "Beta")
    page.get_by_role("button", name="Delete", exact=True).click()   # actionbar
    page.get_by_role("button", name="Delete them").click()          # modal confirm
    expect(page.get_by_role("link", name="Beta")).to_have_count(0)  # gone after reload
    expect(page.get_by_role("link", name="Alpha")).to_be_visible()  # the other survives


def test_copy_creates_new_playlist_after_reload(live_pl_app, page):
    page.goto(f"{live_pl_app}/playlists")
    _select(page, "Alpha")
    page.get_by_role("button", name="Copy…", exact=True).click()   # the duplicate button (not "Copy into…")
    inp = page.get_by_placeholder("New playlist name")
    inp.fill("Alpha Copy")
    inp.press("Enter")
    expect(page.get_by_role("link", name="Alpha Copy")).to_be_visible()   # copy in the table after reload


def test_copy_into_appends_songs_to_existing_playlist(live_pl_app, page):
    page.goto(f"{live_pl_app}/playlists")
    _select(page, "Alpha")                                          # source: Alpha (SongA)
    page.get_by_role("button", name="Copy into…").click()
    page.get_by_role("combobox").select_option(label="Beta")        # destination: Beta
    page.get_by_role("button", name="Copy in", exact=True).click()  # modal confirm -> full reload
    # Beta now holds both songs (SongA copied in alongside its own SongB)
    expect(page.get_by_role("row").filter(has_text="Beta").get_by_role("cell").nth(3)).to_have_text("2")
