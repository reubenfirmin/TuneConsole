"""Live-behavior tests for the server-side merge editor: each control posts to /merge/update
(which mutates the in-memory draft) and re-renders #merge-body; Apply -> HX-Redirect."""
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
def live_merge_app(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_PLAYLIST_HOME", str(tmp_path))
    s = Store(":memory:")
    s.init_schema()
    iid = s.identities.upsert_identity("Main", "c1", None, True)
    a = s.playlists.upsert_playlist(iid, "PLA", "Mix A", 2, "h", 1.0)
    b = s.playlists.upsert_playlist(iid, "PLB", "Mix B", 1, "h", 1.0)
    t1 = s.tracks.upsert_track("v1", "Alpha Song", "X", None, None, 1)
    t2 = s.tracks.upsert_track("v2", "Beta Song", "X", None, None, 1)
    s.playlists.set_playlist_tracks(a, [t1, t2]); s.playlists.set_playlist_tracks(b, [t1])
    client = FakeClient(tracks={"PLA": [_track("v1", "Alpha Song", "X"), _track("v2", "Beta Song", "X")],
                                "PLB": [_track("v1", "Alpha Song", "X")]})
    app = create_app(s, lambda: {iid: client}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield {"base": f"http://127.0.0.1:{port}", "ids": f"{a},{b}", "store": s, "client": client}
    server.should_exit = True
    thread.join(timeout=5)


def test_merge_toggle_updates_count(live_merge_app, page):
    page.goto(f"{live_merge_app['base']}/merge?ids={live_merge_app['ids']}")
    expect(page.get_by_text("2 / 2")).to_be_visible()
    page.locator('input[type=checkbox]').first.uncheck()       # exclude one song
    expect(page.get_by_text("1 / 2")).to_be_visible()          # server re-rendered the count


def test_merge_apply_redirects(live_merge_app, page):
    page.goto(f"{live_merge_app['base']}/merge?ids={live_merge_app['ids']}")
    page.get_by_role("button", name="Apply").click()
    page.wait_for_url("**/cleanup**", timeout=5000)            # HX-Redirect after the merge runs


def test_failed_delivery_keeps_editor_and_source(live_merge_app, page, monkeypatch):
    client = live_merge_app["client"]
    client._tracks["PLA"] = [_track("v1", "Alpha Song", "X")]
    client._tracks["PLB"] = [_track("v1", "Alpha Song", "X"), _track("v2", "Beta Song", "X")]
    monkeypatch.setattr(client, "add_playlist_items", lambda *a: {"status": "STATUS_FAILED"})
    url = f"{live_merge_app['base']}/merge?ids={live_merge_app['ids']}"
    page.goto(url)
    page.get_by_role("button", name="Apply", exact=True).click()
    expect(page.get_by_text("Refusing to delete", exact=False)).to_be_visible()
    expect(page).to_have_url(url)
    expect(page.locator("#merge-body")).to_be_visible()
    assert client.deleted == []
    assert live_merge_app["store"].get_draft(live_merge_app["ids"]) is not None
    assert live_merge_app["store"].get_playlist(int(live_merge_app["ids"].split(",")[1])) is not None


def test_merge_returns_to_the_cleanup_list_before_and_after_applying(live_merge_app, page):
    base = live_merge_app["base"]
    page.goto(f"{base}/cleanup")
    page.get_by_role("link", name="Review", exact=True).first.click()
    back = page.get_by_role("link", name="Back to cleanup")
    expect(back).to_be_visible()
    expect(back).to_have_attribute("href", "/cleanup#overlaps")
    page.locator('input[type=checkbox]').first.uncheck()
    expect(page.get_by_text("1 / 2")).to_be_visible()
    back.click()
    page.wait_for_url("**/cleanup#overlaps")
    page.get_by_role("link", name="Review", exact=True).first.click()
    page.get_by_role("button", name="Apply", exact=True).click()
    page.wait_for_url("**/cleanup?**#overlaps")
    expect(page.get_by_text("Merged into", exact=False)).to_be_visible()


def test_merge_from_playlists_returns_to_playlists(live_merge_app, page):
    base = live_merge_app["base"]
    page.goto(f"{base}/playlists")
    table = page.locator(".playlist-table-card")
    for name in ("Mix A", "Mix B"):
        table.get_by_role("row").filter(has_text=name).get_by_role("checkbox").check()
    page.get_by_role("button", name="Merge", exact=True).click()
    expect(page.get_by_role("link", name="Back to playlists")).to_have_attribute("href", "/playlists")
    page.get_by_role("button", name="Apply", exact=True).click()
    page.wait_for_url("**/playlists?**")
    expect(table.get_by_role("row").filter(has_text="Mix A")).to_be_visible()
    expect(table.get_by_role("row").filter(has_text="Mix B")).to_have_count(0)
