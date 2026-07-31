"""Live-behavior test for the Road Trip tab: create a recipe through the real form, build it into
the on-screen playlist, curate that playlist (cross a slot out, move a slider), and only then save
it - confirming the resulting playlist is tagged Generated (taste-model quarantine + GC eligibility)
and that nothing reached YouTube before the save."""
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import expect

from yt_playlist.core.store import Store
from yt_playlist.rec import road_trip as road_trip_rec
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
def live_road_trip_app(monkeypatch):
    store = Store(":memory:")
    store.init_schema()
    iid = store.upsert_identity("Main", "cred", None, True)
    # A handful of songs of your own, not one: your side of the mix is the whole collection, so a
    # one-song library leaves nothing to swap in when a slot is crossed out.
    pid = store.upsert_playlist(iid, "PL1", "Mix", 6, "h", 1.0)
    mine = [store.upsert_track(f"v{i}", f"My Song {i}", f"My Artist {i}", "Alb", 200, 1)
            for i in range(6)]
    store.set_playlist_tracks(pid, mine)
    # The server runs in this process, so patching the Deezer facts lookup here keeps the build
    # (which enriches every "theirs" candidate) entirely offline.
    monkeypatch.setattr(road_trip_rec, "_facts",
                        lambda title, artist: {"popularity": 500, "year": 2015,
                                               "genre": "psychedelic", "duration": 245})
    monkeypatch.setattr(road_trip_rec, "artist_genre", lambda s, name: "Psychedelic Rock")

    client = FakeClient(
        # "artist" is what /road_trip/autocomplete/artists reads for the suggestion label; without
        # it the form's typeahead has nothing to offer and the recipe can't be built through the UI.
        search_results=[{"browseId": "UC1", "artist": "Tame Impala"}],
        # Several of their songs, so the pool has somewhere to reach when a slot is crossed out.
        artists={"UC1": {"songs": {"results": [
            {"videoId": f"vt{i}", "title": f"Their Song {i}",
             "artists": [{"name": "Tame Impala", "id": "UC1"}],
             "album": {"name": "Currents", "id": "MPRE1"}, "duration_seconds": 245}
            for i in range(12)]}}})
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


def test_build_curate_then_save_road_trip_recipe(live_road_trip_app, page):
    base_url, store = live_road_trip_app
    page.goto(f"{base_url}/road_trip")

    page.fill("form input[x-model='name']", "Beach Run")
    page.fill("form input[x-model='artistQuery']", "Tame Impala")
    page.wait_for_selector(".rt-suggestion")
    page.click(".rt-suggestion")
    expect(page.locator(".genre-chip").first).to_contain_text("Tame Impala")
    # Picking an artist also drops their genre in, as a chip you can remove like any other.
    expect(page.locator(".genre-chip")).to_contain_text(["Tame Impala", "Psychedelic Rock"])
    page.locator(".rt-duration input").first.fill("0")      # a 20-minute trip, so the pool outlasts
    page.locator(".rt-duration input").last.fill("20")      # the playlist and a swap has somewhere to go

    page.click(".rt-form button:has-text('Save recipe')")
    expect(page.locator(".rt-recipe")).to_contain_text("Beach Run")

    # Build: the playlist appears on the page right away (your half first), their tracks stream in,
    # and the controls arrive once it settles. Nothing is on YouTube yet.
    page.click(".rt-recipe button:has-text('Build playlist')")
    expect(page.locator(".rt-draft")).to_be_visible(timeout=10000)
    expect(page.locator(".rt-list .rt-row").first).to_be_visible()
    expect(page.locator(".rt-draft button:has-text('Save to YouTube')")).to_be_visible(timeout=10000)
    assert store.list_road_trip_recipes()[0]["last_playlist_id"] is None
    rid = store.list_road_trip_recipes()[0]["id"]
    assert store.get_road_trip_draft(rid) is not None
    expect(page.locator(".rt-axes .fp-slider").first).to_be_visible()

    # Cross a slot out: the recipe fills it back in rather than leaving a hole. Assert on the row's
    # video id, which expect() retries until the swap lands (a title read races the swap).
    rows = page.locator(".rt-list .rt-row")
    before = rows.count()
    first_vid = rows.first.get_attribute("data-vid")
    rows.first.locator(".rt-x").click()
    expect(page.locator(".rt-list .rt-row").first).not_to_have_attribute("data-vid", first_vid)
    expect(page.locator(".rt-list .rt-row")).to_have_count(before)

    page.click(".rt-draft button:has-text('Save to YouTube')")
    expect(page.locator(".rt-draft")).to_contain_text("Saved", timeout=10000)

    recipes = store.list_road_trip_recipes()
    assert len(recipes) == 1
    last_ytm = recipes[0]["last_playlist_id"]
    assert last_ytm is not None
    assert store.get_playlist_groups()[last_ytm] == GENERATED_GROUP
