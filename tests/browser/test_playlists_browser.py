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
    g = s.upsert_playlist(iid, "PLG", "Gamma", 1, "h", 1.0)
    s.set_playlist_tracks(a, [s.upsert_track("v1", "SongA", "X", None, None, 1)])
    s.set_playlist_tracks(b, [s.upsert_track("v2", "SongB", "Y", None, None, 1)])
    s.set_playlist_tracks(g, [s.upsert_track("v3", "SongC", "Z", None, None, 1)])
    client = FakeClient(tracks={"PLA": [_track("v1", "SongA", "X")],
                                "PLB": [_track("v2", "SongB", "Y")],
                                "PLG": [_track("v3", "SongC", "Z")]})
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


def test_checkbox_cell_click_selects_row(live_pl_app, page):
    # #71: the whole first cell is a click target, not just the small checkbox inside it.
    page.goto(f"{live_pl_app}/playlists")
    cell = page.get_by_role("row").filter(has_text="Alpha").locator("td").first
    cell.click(position={"x": 3, "y": 3})                        # padding area, off the input
    expect(page.get_by_text("1 selected")).to_be_visible()
    cell.click(position={"x": 3, "y": 3})                        # toggles back off
    expect(page.locator(".pl-actionbar")).to_be_hidden()


def test_shift_click_selects_range(live_pl_app, page):
    # #71: click Alpha, then shift-click Gamma -> Beta (between them) comes along.
    page.goto(f"{live_pl_app}/playlists")
    _select(page, "Alpha")
    row = page.get_by_role("row").filter(has_text="Gamma")
    row.get_by_role("checkbox").click(modifiers=["Shift"])
    expect(page.get_by_text("3 selected")).to_be_visible()
    for title in ("Alpha", "Beta", "Gamma"):
        expect(page.get_by_role("row").filter(has_text=title).get_by_role("checkbox")).to_be_checked()


def test_actionbar_labels_are_clean(live_pl_app, page):
    # #74: no arrows or ellipses on the action bar; short labels only.
    page.goto(f"{live_pl_app}/playlists")
    _select(page, "Alpha")
    _select(page, "Beta")
    bar = page.locator(".pl-actionbar")
    for name in ("Merge", "Combine", "Copy into", "Group", "Delete", "Clear"):
        expect(bar.get_by_role("button", name=name, exact=True)).to_be_visible()
    assert "→" not in bar.inner_text() and "…" not in bar.inner_text()


class _SlowDeleteClient(FakeClient):
    """Each remote delete takes a moment, like real YouTube: gives the in-flight UI a window."""
    def delete_playlist(self, playlistId):
        time.sleep(1.5)
        return super().delete_playlist(playlistId)


@pytest.fixture
def live_slow_delete_app(tmp_path):
    old_home = os.environ.get("YT_PLAYLIST_HOME")
    os.environ["YT_PLAYLIST_HOME"] = str(tmp_path)

    s = Store(":memory:")
    s.init_schema()
    iid = s.upsert_identity("Main", "cred", None, True)
    a = s.upsert_playlist(iid, "PLA", "Alpha", 1, "h", 1.0)
    b = s.upsert_playlist(iid, "PLB", "Beta", 1, "h", 1.0)
    s.set_playlist_tracks(a, [s.upsert_track("v1", "SongA", "X", None, None, 1)])
    s.set_playlist_tracks(b, [s.upsert_track("v2", "SongB", "Y", None, None, 1)])
    client = _SlowDeleteClient(tracks={"PLA": [_track("v1", "SongA", "X")],
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


def test_delete_shows_spinner_while_batch_is_in_flight(live_slow_delete_app, page):
    # #72: confirming a batch delete must show progress while the per-playlist remote deletes run,
    # and must not allow a second click. The modal stays up (spinner + disabled buttons) until the
    # server finishes and the page reloads.
    page.goto(f"{live_slow_delete_app}/playlists")
    _select(page, "Alpha")
    _select(page, "Beta")
    page.get_by_role("button", name="Delete", exact=True).click()      # actionbar
    confirm = page.get_by_role("button", name="Delete them")
    confirm.click()                                                    # modal confirm -> in flight
    expect(page.locator(".modal .spinner")).to_be_visible()            # spinner while deleting
    expect(page.get_by_role("button", name="Deleting…")).to_be_disabled()   # no double-submit
    expect(page.get_by_role("button", name="Cancel")).to_be_disabled()      # can't cancel mid-flight
    # both remote deletes finish -> HX-Refresh reload -> rows gone
    expect(page.get_by_role("link", name="Alpha")).to_have_count(0, timeout=15000)
    expect(page.get_by_role("link", name="Beta")).to_have_count(0)


def test_copy_creates_new_playlist_after_reload(live_pl_app, page):
    page.goto(f"{live_pl_app}/playlists")
    _select(page, "Alpha")
    page.get_by_role("button", name="Copy", exact=True).click()   # the duplicate button (not "Copy into")
    inp = page.get_by_placeholder("New playlist name")
    inp.fill("Alpha Copy")
    inp.press("Enter")
    expect(page.get_by_role("link", name="Alpha Copy")).to_be_visible()   # copy in the table after reload


def test_copy_into_appends_songs_to_existing_playlist(live_pl_app, page):
    page.goto(f"{live_pl_app}/playlists")
    _select(page, "Alpha")                                          # source: Alpha (SongA)
    page.get_by_role("button", name="Copy into", exact=True).click()
    page.get_by_role("combobox").select_option(label="Beta")        # destination: Beta
    page.get_by_role("button", name="Copy in", exact=True).click()  # modal confirm -> full reload
    # Beta now holds both songs (SongA copied in alongside its own SongB)
    expect(page.get_by_role("row").filter(has_text="Beta").get_by_role("cell").nth(3)).to_have_text("2")
