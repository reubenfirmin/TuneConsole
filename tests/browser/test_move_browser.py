"""Live-behavior tests for the Move page. Own fixture (two identities + a playlist),
since the shared live_app seeds discover data. Characterization first: these lock the
CURRENT Alpine behavior and must keep passing after the htmx conversion."""
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
def live_move_app():
    s = Store(":memory:")
    s.init_schema()
    i1 = s.upsert_identity("Main", "c1", None, True)
    i2 = s.upsert_identity("Alt", "c2", None, False)
    pid = s.upsert_playlist(i1, "PL1", "Mix", 1, "h", 1.0)
    t = s.upsert_track("v1", "Song", "Artist", None, None, 1)
    s.set_playlist_tracks(pid, [t])
    app = create_app(s, lambda: {i1: FakeClient(), i2: FakeClient()}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield {"base": f"http://127.0.0.1:{port}", "i1": i1, "i2": i2}
    server.should_exit = True
    thread.join(timeout=5)


def _pick(page, i1, i2):
    page.get_by_label("From").select_option(str(i1))
    page.get_by_label("To").select_option(str(i2))


def test_move_copy_keeps_row_and_shows_message(live_move_app, page):
    page.goto(f"{live_move_app['base']}/move")
    _pick(page, live_move_app["i1"], live_move_app["i2"])
    link = page.get_by_role("link", name="Mix ↗")
    expect(link).to_be_visible()
    page.get_by_role("row").filter(has_text="Mix").get_by_role("button", name="Copy").click()
    expect(page.get_by_text("Copied")).to_be_visible()
    expect(link).to_be_visible()                         # copy keeps the row


def test_move_removes_row(live_move_app, page):
    page.goto(f"{live_move_app['base']}/move")
    _pick(page, live_move_app["i1"], live_move_app["i2"])
    page.get_by_role("row").filter(has_text="Mix").get_by_role("button", name="Move").click()
    page.get_by_role("link", name="Mix ↗").wait_for(state="hidden", timeout=3000)   # move deletes -> row gone


def test_move_disabled_for_same_identity(live_move_app, page):
    page.goto(f"{live_move_app['base']}/move")
    i1 = live_move_app["i1"]
    page.get_by_label("From").select_option(str(i1))
    page.get_by_label("To").select_option(str(i1))       # same as From
    expect(page.get_by_text("Pick two different identities")).to_be_visible()
    expect(page.get_by_role("button", name="Move")).to_be_disabled()
