import threading
import time

import pytest
import uvicorn

from yt_playlist.providers.playlist_dates import PlaylistDates
from yt_playlist.web.app import create_app
from tests.browser.conftest import _free_port


@pytest.fixture
def dates_app(store, tmp_path):
    iid = store.upsert_identity("main", "bridge", None, True)
    store.upsert_playlist(iid, "PLold", "Existing playlist", 0, "", 1)
    provider = PlaylistDates(tmp_path)
    app = create_app(store, lambda: {}, playlist_dates=provider)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline
        time.sleep(.02)
    yield f"http://127.0.0.1:{port}", provider
    server.should_exit = True
    thread.join(timeout=5)


@pytest.mark.parametrize("width", [1280, 390])
def test_playlist_dates_connection_is_readable_and_responsive(page, dates_app, tmp_path, width):
    base, provider = dates_app
    page.set_viewport_size({"width": width, "height": 1000})
    page.emulate_media(reduced_motion="reduce")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base + "/setup/playlist-dates", wait_until="networkidle")
    page.get_by_text("First connection: Google project setup").click()
    assert page.get_by_role("button", name="Continue with Google").is_visible()
    assert page.get_by_label("Google Desktop app client").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"playlist-dates-{width}.png"), full_page=True)
    # Once configured, setup details disappear and account actions occupy a compact row.
    provider._save({"client": {"client_id": "test"}, "accounts": {
        "UCme": {"title": "My music account", "error": ""}}})
    page.reload(wait_until="networkidle")
    assert page.get_by_text("My music account").is_visible()
    assert page.get_by_role("button", name="Disconnect").is_visible()
    assert page.locator("input[type=file]").count() == 0
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert errors == []
