"""Characterization tests for the Genres whitelist editor (/genres).

Written against the CURRENT Alpine behavior (parity baseline) and kept passing
after the Alpine->htmx conversion, making the refactor behavior-preserving by
construction. Asserts on user-visible text/roles, not on the transport in use.

The shared browser conftest seeds a playlist, not genres, and is off-limits, so
this module brings its own live_app fixture (the same ~12-line uvicorn boilerplate)
and relies on create_app seeding the built-in genre whitelist.
"""
import re
import socket
import threading
import time

import pytest
import uvicorn

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
def live_app(store):
    # create_app seeds the built-in genre whitelist (incl. "Rock") into the store.
    iid = store.upsert_identity("main", "cred", None, True)
    app = create_app(store, lambda: {iid: FakeClient()}, now_fn=lambda: 1.0)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _count(page):
    """The whitelist size, read from the visible 'N genres' label."""
    text = page.get_by_text(re.compile(r"^\d+ genres$")).inner_text()
    return int(text.split()[0])


def _chip(page, name):
    """The chip whose label is exactly `name` (the inner text span)."""
    return page.get_by_text(name, exact=True)


def test_harness_loads_genres(live_app, page):
    page.goto(f"{live_app}/genres")
    assert page.get_by_role("heading", name="Genre whitelist").is_visible()
    assert _chip(page, "Rock").is_visible()           # a built-in chip is server-rendered


def test_add_via_button_adds_chip_and_bumps_count(live_app, page):
    page.goto(f"{live_app}/genres")
    before = _count(page)
    page.get_by_placeholder("Add a genre…").fill("Phonk")
    page.get_by_role("button", name="Add", exact=True).click()
    _chip(page, "Phonk").wait_for(state="visible", timeout=3000)
    assert _count(page) == before + 1


def test_add_via_enter_adds_chip(live_app, page):
    page.goto(f"{live_app}/genres")
    inp = page.get_by_placeholder("Add a genre…")
    inp.fill("Synthwave")
    inp.press("Enter")
    _chip(page, "Synthwave").wait_for(state="visible", timeout=3000)


def test_remove_chip_drops_it_and_lowers_count(live_app, page):
    page.goto(f"{live_app}/genres")
    before = _count(page)
    # the chip's × button is the sibling of its label span (markup shared across the refactor)
    _chip(page, "Rock").locator("xpath=following-sibling::button").click()
    _chip(page, "Rock").wait_for(state="hidden", timeout=3000)
    assert _count(page) == before - 1


def test_reset_restores_defaults(live_app, page):
    page.goto(f"{live_app}/genres")
    inp = page.get_by_placeholder("Add a genre…")
    inp.fill("Zzztestgenre")
    inp.press("Enter")
    _chip(page, "Zzztestgenre").wait_for(state="visible", timeout=3000)
    page.get_by_role("button", name="Reset to defaults").click()
    _chip(page, "Zzztestgenre").wait_for(state="hidden", timeout=3000)   # custom genre gone
    _chip(page, "Rock").wait_for(state="visible", timeout=3000)          # built-ins restored
