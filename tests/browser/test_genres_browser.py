"""Browser coverage for the genre tag editor under Enrichment / Advanced.

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
from playwright.sync_api import expect

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
    iid = store.identities.upsert_identity("main", "cred", None, True)
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


def _open_editor(live_app, page):
    page.goto(f"{live_app}/enrich")
    expect(page.get_by_role("heading", name="Recognized genre tags")).to_be_hidden()
    page.get_by_text("Advanced", exact=True).click()
    expect(page.get_by_role("heading", name="Recognized genre tags")).to_be_visible()


@pytest.mark.parametrize("width", [1280, 390])
def test_legacy_link_opens_enrichment_editor(live_app, page, width, tmp_path):
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(f"{live_app}/genres")
    expect(page).to_have_url(f"{live_app}/enrich?advanced=genres#recognized-genre-tags")
    assert page.get_by_role("heading", name="Enrichment", exact=True).is_visible()
    assert page.get_by_role("heading", name="Recognized genre tags").is_visible()
    expect(page.locator('.tools-pop a[href="/genres"]')).to_have_count(0)
    house = page.get_by_role("region", name="House", exact=True)
    house.locator("summary").first.click()
    expect(house.get_by_text("Deep House", exact=True)).to_be_visible()
    expect(house.get_by_text("Jazz", exact=True)).to_have_count(0)
    unassigned = page.get_by_role("region", name="No family assigned")
    expect(unassigned.get_by_text("Christian", exact=True)).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=tmp_path / f"genre-hierarchy-{width}.png", full_page=True)


def test_add_via_button_adds_chip_and_bumps_count(live_app, page):
    _open_editor(live_app, page)
    before = _count(page)
    page.get_by_role("textbox", name="Genre tag", exact=True).fill("Whack-a-doodle")
    page.get_by_role("button", name="Add", exact=True).click()
    # The POST replaces the list, including its disclosure elements. Expand
    # the new list only once its updated count proves the swap completed.
    expect(page.get_by_text(re.compile(r"^\d+ genres$"))).to_have_text(f"{before + 1} genres")
    page.get_by_role("region", name="No family assigned").locator("summary").first.click()
    _chip(page, "Whack-a-doodle").wait_for(state="visible", timeout=3000)
    expect(page.get_by_role("region", name="No family assigned").get_by_text(
        "Whack-a-doodle", exact=True)).to_be_visible()
    assert _count(page) == before + 1


def test_add_via_enter_adds_chip(live_app, page):
    _open_editor(live_app, page)
    inp = page.get_by_role("textbox", name="Genre tag", exact=True)
    inp.fill("Synthwave")
    inp.press("Enter")
    page.get_by_role("searchbox", name="Find a genre, alias or family").fill("Synthwave")
    _chip(page, "Synthwave").wait_for(state="visible", timeout=3000)


def test_remove_chip_drops_it_and_lowers_count(live_app, page):
    _open_editor(live_app, page)
    before = _count(page)
    page.get_by_role("region", name="Classic Rock", exact=True).locator("summary").first.click()
    # the chip's × button is the sibling of its label span (markup shared across the refactor)
    _chip(page, "Rock").locator("xpath=following-sibling::button").click()
    _chip(page, "Rock").wait_for(state="hidden", timeout=3000)
    assert _count(page) == before - 1


def test_reset_restores_defaults(live_app, page):
    _open_editor(live_app, page)
    inp = page.get_by_role("textbox", name="Genre tag", exact=True)
    inp.fill("Zzztestgenre")
    inp.press("Enter")
    page.get_by_role("searchbox", name="Find a genre, alias or family").fill("Zzztestgenre")
    _chip(page, "Zzztestgenre").wait_for(state="visible", timeout=3000)
    page.get_by_role("button", name="Reset to defaults").click()
    _chip(page, "Zzztestgenre").wait_for(state="hidden", timeout=3000)   # custom genre gone
    page.get_by_role("searchbox", name="Find a genre, alias or family").fill("Rock")
    _chip(page, "Rock").wait_for(state="visible", timeout=3000)          # built-ins restored


def test_alias_search_and_removal_keep_the_filter(live_app, page):
    _open_editor(live_app, page)
    page.get_by_role("searchbox", name="Find a genre, alias or family").fill("rap")
    _chip(page, "Hip Hop").wait_for(state="visible")
    expect(_chip(page, "Deep House")).to_have_count(0)
    hiphop = _chip(page, "Hip Hop").locator("..")
    hiphop.locator(".genre-aliases summary").click()
    expect(hiphop.locator(".genre-aliases p")).to_contain_text("rap")
    hiphop.get_by_role("button", name="Remove Hip Hop").click()
    expect(_chip(page, "Hip Hop")).to_have_count(0)
    expect(page.get_by_role("searchbox", name="Find a genre, alias or family")).to_have_value("rap")
