"""Live-behavior test for the Setup pairing tab: the token renders verbatim and the connection
indicator starts in the "waiting" state, polling GET /bridge/status via Alpine (see setup.html's
setupForm().init())."""
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect

from yt_playlist.core.store import Store
from yt_playlist.web.app import create_app
from tests.test_web import _FakeRuntime

pytestmark = pytest.mark.browser


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_setup_app():
    s = Store(":memory:")
    s.init_schema()
    rt = _FakeRuntime(s, configured=False)
    app = create_app(s, rt.clients, now_fn=lambda: 1.0, setup=rt)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_setup_pairing_tab_shows_autoconnect_and_waiting_status(live_setup_app, page):
    page.goto(f"{live_setup_app}/setup")
    # Pairing is seamless now: no token to paste, just an auto-connecting extension.
    expect(page.get_by_text("it connects automatically")).to_be_visible()
    # No extension connected yet: GET /bridge/status polls back {"connected": false}
    expect(page.get_by_text("Extension not connected")).to_be_visible()


def test_setup_tab_deep_link_opens_enrichment(live_setup_app, page):
    # #69: the home Last.fm card lands on the Enrichment tab directly, not on Pairing.
    page.goto(f"{live_setup_app}/setup?tab=enrichment")
    expect(page.get_by_role("heading", name="Metadata providers")).to_be_visible()
    expect(page.get_by_text("Pair the browser extension", exact=True)).to_be_hidden()
    # Last.fm signup guidance: the callback URL is optional and the shared secret is irrelevant.
    expect(page.get_by_text("Callback URL")).to_be_visible()
    expect(page.get_by_text("Shared secret")).to_be_visible()


def test_setup_identities_tab_accepts_manual_label(live_setup_app, page):
    page.goto(f"{live_setup_app}/setup")
    page.get_by_role("tab", name="Identities").click()
    page.locator('input[name="label"]').first.fill("Reuben")
    expect(page.locator('input[name="label"]').first).to_have_value("Reuben")
