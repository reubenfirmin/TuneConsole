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
from tests.conftest import FakeClient

pytestmark = pytest.mark.browser


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_merge_app():
    s = Store(":memory:")
    s.init_schema()
    iid = s.upsert_identity("Main", "c1", None, True)
    a = s.upsert_playlist(iid, "PLA", "Mix A", 2, "h", 1.0)
    b = s.upsert_playlist(iid, "PLB", "Mix B", 1, "h", 1.0)
    t1 = s.upsert_track("v1", "Alpha Song", "X", None, None, 1)
    t2 = s.upsert_track("v2", "Beta Song", "X", None, None, 1)
    s.set_playlist_tracks(a, [t1, t2]); s.set_playlist_tracks(b, [t1])
    app = create_app(s, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield {"base": f"http://127.0.0.1:{port}", "ids": f"{a},{b}"}
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
