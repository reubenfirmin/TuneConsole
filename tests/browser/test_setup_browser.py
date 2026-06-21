"""Live-behavior test for the Setup check flow: htmx posts /setup/check, swaps the result,
and fires an `setup-checked` HX-Trigger that Alpine reacts to (auto-fill the master label)."""
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect

from yt_playlist.store import Store
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
    rt.account_name = "Reuben"
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


def test_setup_check_shows_account_and_autofills_master(live_setup_app, page):
    page.goto(f"{live_setup_app}/setup")
    page.locator('textarea[name="headers"]').fill("cookie: x")
    page.get_by_role("button", name="Check sign-in").click()
    # htmx swapped the result fragment in
    expect(page.locator("#check-result")).to_contain_text("Signed in as")
    expect(page.locator("#check-result")).to_contain_text("Reuben")
    # HX-Trigger -> Alpine onChecked auto-filled the (blank) master label input
    expect(page.locator('input[name="label"]').first).to_have_value("Reuben")
