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
