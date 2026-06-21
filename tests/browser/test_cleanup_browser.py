"""Live-behavior tests for the Cleanup page, section by section. Own fixture.
Characterization first: lock current Alpine behavior, keep passing after htmx conversion."""
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
def live_cleanup_app():
    s = Store(":memory:")
    s.init_schema()
    iid = s.upsert_identity("Main", "c1", None, True)
    s.upsert_playlist(iid, "PLE", "Empty One", 0, "h", 1.0)   # no tracks -> an empty playlist
    # two identical playlists -> an exact-duplicate group
    dup_tracks = [s.upsert_track(f"d{i}", f"D{i}", "X", None, None, 1) for i in range(2)]
    for ytm, title in (("PLD1", "Dup A"), ("PLD2", "Dup B")):
        pid = s.upsert_playlist(iid, ytm, title, 2, "h", 1.0)
        s.set_playlist_tracks(pid, dup_tracks)
    # two playlists that share tracks but aren't identical -> an overlap pair
    ov = [s.upsert_track(f"o{i}", f"O{i}", "X", None, None, 1) for i in range(4)]
    oa = s.upsert_playlist(iid, "POVA", "Ov A", 3, "h", 1.0); s.set_playlist_tracks(oa, ov[0:3])
    ob = s.upsert_playlist(iid, "POVB", "Ov B", 3, "h", 1.0); s.set_playlist_tracks(ob, ov[1:4])
    app = create_app(s, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_empty_playlist_delete_removes_row(live_cleanup_app, page):
    page.goto(f"{live_cleanup_app}/cleanup")
    link = page.get_by_role("link", name="Empty One ↗")
    expect(link).to_be_visible()
    page.get_by_role("row").filter(has_text="Empty One").get_by_role("button", name="Delete").click()
    link.wait_for(state="hidden", timeout=3000)


def test_keep_one_resolves_duplicate_group(live_cleanup_app, page):
    page.goto(f"{live_cleanup_app}/cleanup")
    expect(page.get_by_text("identical copies")).to_be_visible()
    page.get_by_role("button", name="Keep this one").first.click()
    # keeping one deletes the other copy and the group resolves (page recomputes)
    page.get_by_text("identical copies").wait_for(state="hidden", timeout=4000)


def test_overlap_hide_pair_via_pie_menu(live_cleanup_app, page):
    page.goto(f"{live_cleanup_app}/cleanup")
    row = page.locator("tbody.ov-row")
    expect(row).to_have_count(1)
    row.locator(".kebab").click()                                   # open the radial pie menu
    row.locator(".wedge.w-hide").dispatch_event("click")           # "hide this pair" wedge
    # htmx.ajax -> HX-Refresh -> reload; the pair is now suppressed and appears under "Hidden"
    expect(page.get_by_role("heading", name="Hidden overlaps")).to_be_visible()
    expect(page.locator("tbody.ov-row")).to_have_count(0)          # gone from the Overlaps table
