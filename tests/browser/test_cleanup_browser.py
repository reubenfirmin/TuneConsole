"""Live-behavior tests for the Cleanup page, section by section. Own fixture.
Characterization first: lock current Alpine behavior, keep passing after htmx conversion."""
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
def live_cleanup_app(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    s = Store(":memory:")
    s.init_schema()
    iid = s.identities.upsert_identity("Main", "c1", None, True)
    s.playlists.upsert_playlist(iid, "PLE", "Empty One", 0, "h", 1.0)   # no tracks -> an empty playlist
    # two identical playlists -> an exact-duplicate group
    dup_tracks = [s.tracks.upsert_track(f"d{i}", f"D{i}", "X", None, None, 1) for i in range(2)]
    for ytm, title in (("PLD1", "Dup A"), ("PLD2", "Dup B")):
        pid = s.playlists.upsert_playlist(iid, ytm, title, 2, "h", 1.0)
        s.playlists.set_playlist_tracks(pid, dup_tracks)
    # two playlists that share tracks but aren't identical -> an overlap pair
    ov = [s.tracks.upsert_track(f"o{i}", f"O{i}", "X", None, None, 1) for i in range(4)]
    oa = s.playlists.upsert_playlist(iid, "POVA", "Ov A", 3, "h", 1.0); s.playlists.set_playlist_tracks(oa, ov[0:3])
    ob = s.playlists.upsert_playlist(iid, "POVB", "Ov B", 3, "h", 1.0); s.playlists.set_playlist_tracks(ob, ov[1:4])
    # Three near-duplicates share seven tracks and each have a different eighth track.
    near = [s.tracks.upsert_track(f"n{i}", f"N{i}", "X", None, None, 1) for i in range(10)]
    for n, ytm in enumerate(("PNEARA", "PNEARB", "PNEARC")):
        pid = s.playlists.upsert_playlist(iid, ytm, f"Near {chr(65 + n)}", 8, "h", 1.0)
        s.playlists.set_playlist_tracks(pid, near[:7] + [near[7 + n]])
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
    link = page.get_by_role("link", name="Empty One", exact=True)
    expect(link).to_be_visible()
    page.get_by_role("row").filter(has_text="Empty One").get_by_role("button", name="Delete").click()
    link.wait_for(state="hidden", timeout=3000)


def test_empty_playlist_failed_read_keeps_row_and_allows_retry(live_cleanup_app, page, monkeypatch):
    from yt_playlist.library import executor
    monkeypatch.setattr(executor, "with_retry", lambda fn: fn())
    get_playlist = FakeClient.get_playlist

    def unreadable(self, playlistId, limit=None):
        if playlistId == "PLE":
            raise TimeoutError("YouTube read timed out")
        return get_playlist(self, playlistId, limit)

    monkeypatch.setattr(FakeClient, "get_playlist", unreadable)
    page.goto(f"{live_cleanup_app}/cleanup")
    row = page.get_by_role("row").filter(has_text="Empty One")
    row.get_by_role("button", name="Delete", exact=True).click()
    expect(page.locator(".toast-err")).to_contain_text("empty on YouTube")
    expect(row).to_be_visible()
    expect(row.get_by_role("button", name="Delete", exact=True)).to_be_enabled()

    monkeypatch.setattr(FakeClient, "get_playlist", get_playlist)
    row.get_by_role("button", name="Delete", exact=True).click()
    expect(row).to_have_count(0)


def test_keep_one_resolves_duplicate_group(live_cleanup_app, page):
    page.goto(f"{live_cleanup_app}/cleanup")
    expect(page.get_by_text("identical copies")).to_be_visible()
    page.get_by_role("button", name="Keep this one").first.click()
    # keeping one deletes the other copy and the group resolves (page recomputes)
    page.get_by_text("identical copies").wait_for(state="hidden", timeout=4000)


def test_overlap_hide_pair_via_pie_menu(live_cleanup_app, page):
    page.goto(f"{live_cleanup_app}/cleanup")
    row = page.locator('[data-section="overlaps"] tbody.ov-row')
    expect(row).to_have_count(1)
    row.locator(".kebab").click()                                   # open the radial pie menu
    row.locator(".wedge.w-hide").dispatch_event("click")           # "hide this pair" wedge
    # htmx.ajax -> HX-Refresh -> reload; the pair is now suppressed and appears under "Hidden"
    expect(page.get_by_role("button", name="Hidden overlaps 1")).to_be_visible()
    expect(page.locator('[data-section="overlaps"] tbody.ov-row')).to_have_count(0)
    expect(page.locator('[data-section="near"] tbody.ov-row')).to_have_count(3)


def test_near_duplicate_hide_and_restore_via_same_pie_menu(live_cleanup_app, page):
    page.goto(f"{live_cleanup_app}/cleanup")
    rows = page.locator('[data-section="near"] tbody.ov-row')
    expect(rows).to_have_count(3)
    row = rows.first
    row.get_by_role("button", name="More actions").click()
    expect(row.get_by_role("menu", name="Overlap actions")).to_be_visible()
    row.locator(".wedge.w-hide").click()
    expect(rows).to_have_count(2)
    expect(page.locator('[data-section="overlaps"] tbody.ov-row')).to_have_count(1)
    page.get_by_role("button", name="Hidden overlaps 1").click()
    page.get_by_role("button", name="Unhide", exact=True).click()
    expect(rows).to_have_count(3)


def test_near_duplicate_bulk_hide_only_affects_its_table(live_cleanup_app, page):
    page.goto(f"{live_cleanup_app}/cleanup")
    rows = page.locator('[data-section="near"] tbody.ov-row')
    rows.first.get_by_role("button", name="More actions").click()
    rows.first.locator(".wedge.w-below").click()
    expect(page.get_by_role("heading", name="Hide these pairs?")).to_be_visible()
    page.get_by_role("button", name="Yes, hide them").click()
    expect(rows).to_have_count(0)
    expect(page.locator('[data-section="overlaps"] tbody.ov-row')).to_have_count(1)


def test_near_duplicate_review_returns_to_near_section(live_cleanup_app, page):
    page.goto(f"{live_cleanup_app}/cleanup")
    page.locator('[data-section="near"]').get_by_role("link", name="Review", exact=True).first.click()
    back = page.get_by_role("link", name="Back to cleanup")
    expect(back).to_have_attribute("href", "/cleanup#near")
    back.click()
    expect(page).to_have_url(f"{live_cleanup_app}/cleanup#near")
