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
def live_pl_data(tmp_path):
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
    app = create_app(s, lambda: {iid: client}, now_fn=lambda: 2_000_000)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield {"base": f"http://127.0.0.1:{port}", "store": s, "identity": iid}
    server.should_exit = True
    thread.join(timeout=5)
    if old_home is None:
        os.environ.pop("YT_PLAYLIST_HOME", None)
    else:
        os.environ["YT_PLAYLIST_HOME"] = old_home


@pytest.fixture
def live_pl_app(live_pl_data):
    return live_pl_data["base"]


@pytest.fixture
def recent_pl_app(live_pl_data):
    store, iid = live_pl_data["store"], live_pl_data["identity"]
    for ytm, title, age in [
        ("NEW", "Zulu work in progress", 3600),
        ("GROOVES", "Evening grooves — funk, soul and everything in between", 2 * 86400),
        ("GENERATED", "From your catalog — September 20, 2026", 3 * 86400),
        ("BOUNDARY", "Seven days", 7 * 86400),
        ("OLDER", "Older draft", 7 * 86400 + 1),
        ("HIDDEN", "Hidden draft", 60),
        ("LM", "Liked Music", 60),
    ]:
        store.upsert_playlist(iid, ytm, title, 0, "", 2_000_000, created_at=2_000_000 - age)
    for title in ("303", "Brexit List"):
        store.upsert_playlist(iid, title, title, 0, "", 2_000_000)
    store.set_playlist_group("GENERATED", "Generated")
    store.set_playlist_group("PLA", "Faves")
    store.hide_playlist("HIDDEN")
    return live_pl_data["base"]


def _select(page, title):
    page.locator(".playlist-table-card").get_by_role("row").filter(has_text=title).get_by_role("checkbox").check()


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
    bar = page.locator(".pl-actionbar")
    expect(bar.get_by_role("button", name="Merge", exact=True)).to_be_hidden()
    _select(page, "Beta")
    for name in ("Merge", "Combine", "Copy into", "Group", "Delete", "Deselect all"):
        expect(bar.get_by_role("button", name=name, exact=True)).to_be_visible()
    expect(bar.get_by_role("button", name="Deselect all", exact=True)).to_have_attribute(
        "title", "Deselect all selected playlists")
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
    expect(page.locator(".playlist-table-card").get_by_role("link", name="Alpha Copy")).to_be_visible()
    expect(page.get_by_role("region", name="New playlists").get_by_role("link", name="Alpha Copy")).to_be_visible()


def test_copy_into_appends_songs_to_existing_playlist(live_pl_app, page):
    page.goto(f"{live_pl_app}/playlists")
    _select(page, "Alpha")                                          # source: Alpha (SongA)
    page.get_by_role("button", name="Copy into", exact=True).click()
    page.get_by_role("combobox").select_option(label="Beta")        # destination: Beta
    page.get_by_role("button", name="Copy in", exact=True).click()  # modal confirm -> full reload
    # Beta now holds both songs (SongA copied in alongside its own SongB)
    expect(page.get_by_role("row").filter(has_text="Beta").get_by_role("cell").nth(3)).to_have_text("2")


def test_recent_playlists_are_duplicate_shortcuts_with_shared_selection(recent_pl_app, page):
    page.goto(f"{recent_pl_app}/playlists")
    recent = page.get_by_role("region", name="New playlists")
    main = page.locator(".playlist-table-card")
    titles = ["Zulu work in progress", "Evening grooves — funk, soul and everything in between",
              "Seven days"]
    expect(recent.locator(".ptitle")).to_have_text(titles)
    main.get_by_role("button", name="Playlist").click()
    expect(recent.locator(".ptitle")).to_have_text(titles)  # newest first regardless of the main sort
    for title in titles:
        expect(main.get_by_role("link", name=title, exact=True)).to_be_visible()
    recent.get_by_role("row").filter(has_text="Zulu").get_by_role("checkbox").check()
    expect(main.get_by_role("row").filter(has_text="Zulu").get_by_role("checkbox")).to_be_checked()
    expect(page.locator(".pl-actionbar")).to_contain_text("1 selected")
    main.get_by_role("row").filter(has_text="Zulu").get_by_role("checkbox").uncheck()
    expect(recent.get_by_role("row").filter(has_text="Zulu").get_by_role("checkbox")).not_to_be_checked()
    recent.get_by_role("button", name="New playlists", exact=False).click()
    expect(recent).to_be_hidden()
    shortcut = page.locator(".playlist-shortcuts").get_by_role("button", name="New playlists", exact=False)
    expect(shortcut).to_be_visible()
    page.reload()
    expect(recent).to_be_hidden()
    shortcut.click()
    expect(recent.locator(".ptitle")).to_have_text(titles)


def test_shift_selection_uses_clicked_occurrence_of_duplicate(recent_pl_app, page):
    page.goto(f"{recent_pl_app}/playlists")
    main = page.locator(".playlist-table-card")
    _select(page, "Evening grooves")
    main.get_by_role("row").filter(has_text="Gamma").get_by_role("checkbox").click(modifiers=["Shift"])
    expect(page.locator(".pl-actionbar")).to_contain_text("2 selected")
    expect(main.get_by_role("row").filter(has_text="Zulu").get_by_role("checkbox")).not_to_be_checked()
    page.get_by_role("button", name="Deselect all", exact=True).click()
    recent = page.get_by_role("region", name="New playlists")
    recent.get_by_role("row").filter(has_text="Zulu").get_by_role("checkbox").check()
    main.get_by_role("row").filter(has_text="Alpha").get_by_role("checkbox").click(modifiers=["Shift"])
    expect(page.locator(".pl-actionbar")).to_contain_text("5 selected")  # three new rows, 303, Alpha


@pytest.mark.parametrize("width", [1280, 390])
def test_recent_and_generated_layout_and_promotion(recent_pl_app, page, width, tmp_path):
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{recent_pl_app}/playlists")
    page.wait_for_load_state("networkidle")
    recent, generated = page.locator(".playlist-new"), page.locator(".playlist-generated")
    recent_box = recent.bounding_box()
    for section in [recent, page.locator(".playlist-table-card")]:
        box = section.get_by_role("checkbox").first.bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width
    page.screenshot(path=tmp_path / f"new-playlists-{width}.png", full_page=True)
    expect(recent.get_by_role("button", name="Promote to library", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Generated playlists", exact=False).click()
    expect(recent).to_be_hidden()
    assert generated.bounding_box()["y"] == pytest.approx(recent_box["y"], abs=1)
    assert generated.bounding_box()["width"] == recent_box["width"]
    row = generated.get_by_role("row").filter(has_text="From your catalog")
    title, action = row.locator(".ptitle"), row.get_by_role("button", name="Promote to library", exact=True)
    title_box, action_box = title.bounding_box(), action.bounding_box()
    assert action_box["y"] >= title_box["y"] + title_box["height"]
    assert title_box["width"] > 0
    assert row.locator(".generated-title-actions").evaluate("el => el.scrollWidth <= el.clientWidth")
    generated.get_by_role("button", name="Generated playlists", exact=False).click()
    shortcuts = page.locator(".playlist-shortcuts")
    expect(shortcuts.get_by_role("button")).to_have_count(2)
    for button in shortcuts.get_by_role("button").all():
        expect(button).to_be_visible()
    assert shortcuts.bounding_box()["x"] + shortcuts.bounding_box()["width"] <= width
    shortcuts.get_by_role("button", name="Generated playlists", exact=False).click()
    page.screenshot(path=tmp_path / f"recent-playlists-{width}.png", full_page=True)
    generated.get_by_role("button", name="Promote to library", exact=True).click()
    expect(page.locator(".playlist-table-card").get_by_role("link", name="From your catalog", exact=False)).to_be_visible()
    shortcuts.get_by_role("button", name="New playlists", exact=False).click()
    expect(recent.locator(".ptitle")).to_have_count(4)
    expect(generated).to_be_hidden()


def test_no_recent_playlists_hides_the_section(live_pl_app, page):
    page.goto(f"{live_pl_app}/playlists")
    expect(page.get_by_role("region", name="New playlists")).to_be_hidden()
