"""Live-behavior test for the Road Trip tab: create a recipe through the real form, generate it,
and confirm the resulting playlist is tagged Generated (taste-model quarantine + GC eligibility)."""
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect

from yt_playlist.core.store import Store
from yt_playlist.repos.rec_query import GENERATED_GROUP
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
def live_road_trip_app():
    store = Store(":memory:")
    store.init_schema()
    iid = store.upsert_identity("Main", "cred", None, True)
    pid = store.upsert_playlist(iid, "PL1", "Mix", 1, "h", 1.0)
    t0 = store.upsert_track("v0", "My Song", "My Artist", "Alb", 200, 1)
    store.set_playlist_tracks(pid, [t0])

    client = FakeClient(
        search_results=[{"browseId": "UC1"}],
        artists={"UC1": {"songs": {"results": [
            {"videoId": "vt0", "title": "Their Song",
             "artists": [{"name": "Tame Impala", "id": "UC1"}],
             "album": {"name": "Currents", "id": "MPRE1"}, "duration_seconds": 245},
        ]}}})
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 1000.0)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", store
    server.should_exit = True
    thread.join(timeout=5)


def test_create_and_generate_road_trip_recipe(live_road_trip_app, page):
    base_url, store = live_road_trip_app
    page.goto(f"{base_url}/road_trip")

    page.fill("form input[x-model='name']", "Beach Run")
    page.fill("form input[x-model='artistQuery']", "Tame Impala")
    page.wait_for_selector(".rt-suggestion")
    page.click(".rt-suggestion")
    expect(page.locator(".genre-chip")).to_contain_text("Tame Impala")

    page.click("form button:has-text('Save recipe')")
    expect(page.locator(".rt-recipe")).to_contain_text("Beach Run")

    page.click(".rt-recipe button:has-text('Generate')")
    expect(page.locator(".rt-recipe")).to_contain_text("Last generated", timeout=10000)

    recipes = store.list_road_trip_recipes()
    assert len(recipes) == 1
    last_ytm = recipes[0]["last_playlist_id"]
    assert last_ytm is not None
    assert store.get_playlist_groups()[last_ytm] == GENERATED_GROUP
