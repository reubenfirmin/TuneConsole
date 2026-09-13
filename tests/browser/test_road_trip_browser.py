"""Live-behavior test for the Road Trip tab: create a recipe through the real form, build it into
the on-screen playlist, curate that playlist (cross a slot out, move a slider), and only then save
it - confirming the resulting playlist is tagged Generated (taste-model quarantine + GC eligibility)
and that nothing reached YouTube before the save."""
import socket
import threading
import time
from urllib.parse import parse_qs

import pytest
import uvicorn
from playwright.sync_api import expect

from yt_playlist.core.store import Store
from yt_playlist.rec import road_trip as road_trip_rec
from yt_playlist.repos.rec_query import GENERATED_GROUP
from yt_playlist.web.app import create_app
from yt_playlist.web.routes import road_trip as road_trip_route
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
    # Picking an artist auto-fills their GENRE, and a genre input resolves through Last.fm and
    # MusicBrainz to find that genre's top artists. Stub it: without this the build makes real
    # network calls, which is both wrong for a test and slow enough to miss the timeout.
    monkeypatch.setattr(road_trip_rec, "genre_artists",
                        lambda store, genre, decade=None, limit=12: ["Tame Impala"])

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


def test_build_curate_then_save_road_trip_recipe(live_road_trip_app, page, monkeypatch):
    base_url, store = live_road_trip_app
    page.goto(f"{base_url}/road_trip")

    page.fill("form input[x-model='name']", "Beach Run")
    page.locator(".rt-duration input").first.fill("0")      # a 20-minute trip, so the pool outlasts
    page.locator(".rt-duration input").last.fill("20")      # the playlist and a swap has somewhere to go
    page.click("button:has-text('Choose passenger music')")
    page.fill("form input[x-model='artistQuery']", "Tame Impala")
    page.wait_for_selector(".rt-suggestion")
    page.click(".rt-suggestion")
    expect(page.locator(".genre-chip").first).to_contain_text("Tame Impala")
    # Picking an artist also drops their genre in, as a chip you can remove like any other.
    expect(page.locator(".genre-chip")).to_contain_text(["Tame Impala", "Psychedelic Rock"])
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

    # Every mix control saves and replaces the editor. Keep the section the user chose open
    # across those swaps, and verify the edits still reach the recipe.
    mix = page.locator(".rt-step").filter(has=page.locator("summary strong", has_text="Shape the mix"))
    passenger = page.locator(".rt-step").filter(has=page.locator("summary strong", has_text="Passenger music"))
    mix.locator("summary").click()
    edits = [
        ("#rt-balance", "ArrowRight", "own_pct", 45),
        ("#rt-familiarity", "ArrowRight", "familiarity_pct", 55),
        ("input[aria-label='hours']", "ArrowUp", "target_minutes", 80),
        ("input[aria-label='minutes']", "ArrowUp", "target_minutes", 85),
        (".rt-preset:has-text('30 min')", None, "target_minutes", 30),
    ]
    for selector, key, field, expected in edits:
        old_form = page.locator(".rt-form").element_handle()
        control = page.locator(selector)
        if key:
            control.press(key)
            control.press("Tab")  # commit number-input changes without choosing another section
        else:
            control.click()
        page.wait_for_function("form => !form.isConnected", arg=old_form)
        expect(mix).to_have_js_property("open", True)
        expect(passenger).to_have_js_property("open", False)
        assert store.get_road_trip_recipe(rid)[field] == expected

    page.get_by_role("button", name="Choose passenger music").click()
    expect(passenger).to_have_js_property("open", True)
    expect(mix).to_have_js_property("open", False)
    expect(passenger.locator(".rt-axis-context").first).to_contain_text("Tame Impala")

    # Genre and year sliders share the recipe form. Moving one must send its own value,
    # without the hidden sliders in another section overriding it.
    genre_slider = passenger.locator(".fp-slider").first
    old_form = page.locator(".rt-form").element_handle()
    with page.expect_request(f"**/road_trip/draft/{rid}/tilt/v2") as request:
        genre_slider.press("Home")
    posted = parse_qs(request.value.post_data)
    assert posted["share"] == ["0"]
    page.wait_for_function("form => !form.isConnected", arg=old_form)
    expect(genre_slider).to_have_value("0")
    expect(passenger).to_have_js_property("open", True)
    state = store.get_road_trip_draft(rid)
    assert state["targets"]["theirs"][posted["axis"][0]] == 0.0
    assert state["targets"]["mine"] == {}
    assert state["stats"]["their_count"] == 0

    # Releasing that pin restores the passenger tracks before continuing to curate the mix.
    old_form = page.locator(".rt-form").element_handle()
    passenger.get_by_role("button", name="Reset genre balance").click()
    page.wait_for_function("form => !form.isConnected", arg=old_form)
    expect(genre_slider).to_have_value("100")
    assert store.get_road_trip_draft(rid)["targets"]["theirs"] == {}

    passenger.locator("summary").press("Enter")
    expect(page.locator(".rt-step[open]")).to_have_count(0)
    mix.locator("summary").press("Enter")
    expect(mix).to_have_js_property("open", True)

    # Cross a slot out: the recipe fills it back in rather than leaving a hole. Assert on the row's
    # video id, which expect() retries until the swap lands (a title read races the swap).
    rows = page.locator(".rt-list .rt-row")
    before = rows.count()
    first_vid = rows.first.get_attribute("data-vid")
    rows.first.locator(".rt-x").click()
    expect(page.locator(".rt-list .rt-row").first).not_to_have_attribute("data-vid", first_vid)
    expect(page.locator(".rt-list .rt-row")).to_have_count(before)
    expect(mix).to_have_js_property("open", True)

    saving, release = threading.Event(), threading.Event()
    create_playlist = road_trip_route.executor.create_generated_playlist

    def slow_save(*args, **kwargs):
        saving.set()
        assert release.wait(15), "test did not release the YouTube save"
        return create_playlist(*args, **kwargs)

    monkeypatch.setattr(road_trip_route.executor, "create_generated_playlist", slow_save)
    try:
        with page.expect_popup() as opened:
            page.get_by_role("button", name="Save to YouTube").click()
        loading = opened.value
        expect(loading).to_have_url(f"{base_url}/home/generating")
        expect(loading.get_by_role("heading", name="Wiring up your playlist")).to_be_visible()
        expect(page.get_by_role("button", name="Save to YouTube")).to_be_disabled()
        assert saving.is_set()
        assert store.get_road_trip_recipe(rid)["last_playlist_id"] is None
    finally:
        release.set()
    page.wait_for_url("**/playlist/*", timeout=10000)
    assert loading.is_closed()

    recipes = store.list_road_trip_recipes()
    assert len(recipes) == 1
    last_ytm = recipes[0]["last_playlist_id"]
    assert last_ytm is not None
    assert store.get_playlist_groups()[last_ytm] == GENERATED_GROUP
    playlist = next(p for p in store.get_playlists() if p.ytm_playlist_id == last_ytm)
    expect(page).to_have_url(f"{base_url}/playlist/{playlist.id}")


def test_failed_save_closes_loading_screen_and_keeps_the_draft(live_road_trip_app, page, monkeypatch):
    base_url, store = live_road_trip_app
    rid = store.save_road_trip_recipe(None, "Save failure", 100, [], [], 20, 1000)
    state = road_trip_rec.start_draft(store, store.get_road_trip_recipe(rid), 1000, seed=7)
    road_trip_rec.finish_draft(state, store, 1000)
    store.save_road_trip_draft(rid, state, 1000)
    release = threading.Event()

    def failed_save(*args, **kwargs):
        assert release.wait(15), "test did not release the YouTube save"
        raise RuntimeError("YouTube unavailable")

    monkeypatch.setattr(road_trip_route.executor, "create_generated_playlist", failed_save)
    page.goto(f"{base_url}/road_trip")
    try:
        with page.expect_popup() as opened:
            page.get_by_role("button", name="Save to YouTube").click()
        loading = opened.value
        expect(loading.get_by_role("heading", name="Wiring up your playlist")).to_be_visible()
    finally:
        release.set()
    expect(page.locator(".gen-done")).to_contain_text("Couldn't save")
    expect(page.get_by_role("button", name="Save to YouTube")).to_be_enabled()
    assert loading.is_closed()
    expect(page).to_have_url(f"{base_url}/road_trip")
    expect(page.locator(".rt-row")).to_have_count(len(state["picked"]))
    assert store.get_road_trip_draft(rid)["saved_playlist_id"] is None


def test_passenger_balance_stays_visible_during_background_search(live_road_trip_app, page, monkeypatch):
    base_url, store = live_road_trip_app
    rid = store.save_road_trip_recipe(None, "Three genres", 0, [], ["Rock", "Pop", "Jazz"], 100, 1000)
    state = road_trip_rec.start_draft(store, store.get_road_trip_recipe(rid), 1000, seed=1)
    state["pool"] = [road_trip_rec._candidate(
        f"{genre}-{i}", f"Song {i}", f"Artist {genre} {i}", "", None, 300, "theirs", genre, 1995)
        for genre, count in [("Rock", 5), ("Pop", 3), ("Jazz", 2)] for i in range(count)]
    for candidate in state["pool"]:
        candidate["score"] = 1.0
    state["own_facts_left"] = 0
    state["targets"]["theirs"] = {"genre:Rock": .5, "genre:Pop": .3, "genre:Jazz": .2}
    road_trip_rec.finish_draft(state, store, 1000)
    store.save_road_trip_draft(rid, state, 1000)
    held = []
    monkeypatch.setattr(road_trip_route, "_spawn", held.append)
    page.goto(f"{base_url}/road_trip")
    passenger = page.locator(".rt-step").filter(has=page.locator("summary strong", has_text="Passenger music"))
    sliders = passenger.locator("input[type=range]")
    expect(sliders).to_have_count(3)
    expect(sliders.first).to_have_value("50")

    # Multiple pointer positions in a single drag keep the original siblings' ratio. Preview
    # happens before the change event sends a request, and the server must return the same values.
    for value in (30, 20):
        sliders.first.evaluate("(el, value) => { el.value = value; el.dispatchEvent(new Event('input', {bubbles: true})); }", value)
    for slider, value in zip(sliders.all(), ["20", "48", "32"]):
        expect(slider).to_have_value(value)
    assert store.get_road_trip_draft(rid)["targets"]["theirs"]["genre:Rock"] == .5

    try:
        old_form = page.locator(".rt-form").element_handle()
        sliders.first.dispatch_event("change")
        page.wait_for_function("form => !form.isConnected", arg=old_form)
        expect(passenger).to_have_js_property("open", True)
        expect(sliders).to_have_count(3)
        for slider, value in zip(sliders.all(), ["20", "48", "32"]):
            expect(slider).to_be_visible()
            expect(slider).to_be_disabled()
            expect(slider).to_have_value(value)
        assert held
        assert store.get_road_trip_draft(rid)["targets"]["theirs"] == {
            "genre:Rock": .2, "genre:Pop": .48, "genre:Jazz": .32}
    finally:
        for worker in held:
            worker()

    expect(sliders.first).to_be_enabled(timeout=10000)
    for slider, value in zip(sliders.all(), ["20", "48", "32"]):
        expect(slider).to_have_value(value)
    page.reload()
    for slider, value in zip(sliders.all(), ["20", "48", "32"]):
        expect(slider).to_have_value(value)


@pytest.mark.parametrize("addition", ["genre", "artist"])
def test_adding_passenger_music_updates_sliders_before_tracks_arrive(live_road_trip_app, page, monkeypatch, addition):
    base_url, store = live_road_trip_app
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    genres = ["Rock", "Pop", "Jazz"]
    rid = store.save_road_trip_recipe(None, "Add music", 0, [], genres, 100, 1000)
    state = road_trip_rec.start_draft(store, store.get_road_trip_recipe(rid), 1000, seed=7)
    state["pool"] = [road_trip_rec._candidate(
        f"{genre}-{i}", f"Song {i}", f"Artist {genre} {i}", "", None, 300, "theirs", genre, 1995)
        for genre in genres for i in range(12)]
    for candidate in state["pool"]:
        candidate["score"] = 1
    state["targets"]["theirs"] = {"genre:" + g: p for g, p in zip(genres, [.6, .3, .1])}
    state["done_inputs"] = genres.copy()
    state["own_facts_left"] = 0
    road_trip_rec.finish_draft(state, store, 1000)
    store.save_road_trip_draft(rid, state, 1000)
    held = []
    monkeypatch.setattr(road_trip_route, "_spawn", held.append)
    monkeypatch.setattr(road_trip_rec, "artist_genre",
                        lambda store, name: "Classic Rock" if name == "Journey" else None)
    page.route("**/road_trip/autocomplete/artists?*",
               lambda route: route.fulfill(json={"results": ["Journey"]}))

    def songs(client, kind, name, cap, store=None, decade=None):
        genre = "Classic Rock" if kind == "artist" else name
        return ([{"video_id": f"new-{name}-{i}", "title": f"New song {i}",
                  "artist": name if kind == "artist" else f"{name} Artist {i}", "album": "",
                  "thumbnail": None, "duration": 300, "genre": genre} for i in range(8)], [])

    monkeypatch.setattr(road_trip_rec, "other_input_songs", songs)
    page.goto(f"{base_url}/road_trip")
    passenger = page.locator(".rt-step").filter(has=page.locator("summary strong", has_text="Passenger music"))
    new_genre = "Funk" if addition == "genre" else "Classic Rock"
    try:
        old_form = page.locator(".rt-form").element_handle()
        if addition == "genre":
            passenger.get_by_role("combobox", name="Add a passenger genre").fill("Funk")
            passenger.locator(".rt-suggestion").get_by_text("funk", exact=True).click()
        else:
            passenger.locator("input[x-model=artistQuery]").fill("Journey")
            passenger.get_by_role("button", name="Journey", exact=True).click()
        page.wait_for_function("form => !form.isConnected", arg=old_form)
        assert not errors
        assert held
        waiting = store.get_road_trip_draft(rid)
        assert waiting["building"]
        assert not any(c["genre"] == new_genre for c in waiting["pool"])
        assert new_genre in {road_trip_rec._canon_genre(g) for g in store.get_road_trip_recipe(rid)["genres"]}
        if addition == "artist":
            expect(passenger.locator(".genre-chip.is-theirs").filter(has_text="Classic Rock like Journey")).to_be_visible()
            expect(passenger.locator(".rt-axis-context").filter(has_text="Like Journey")).to_be_visible()
        for genre, pct in zip(genres + [new_genre], [45, 23, 7, 25]):
            slider = passenger.get_by_role("slider", name=genre, exact=True)
            expect(slider).to_be_visible()
            expect(slider).to_be_disabled()
            expect(slider).to_have_value(str(pct))
    finally:
        for worker in held:
            worker()
    expect(passenger.get_by_role("slider", name=new_genre, exact=True)).to_be_enabled(timeout=10000)
    assert any(c["genre"] == new_genre for c in store.get_road_trip_draft(rid)["picked"])
    page.reload()
    for genre, pct in zip(genres + [new_genre], [45, 23, 7, 25]):
        expect(passenger.get_by_role("slider", name=genre, exact=True)).to_have_value(str(pct))
    if addition == "artist":
        expect(passenger.locator(".genre-chip.is-theirs").filter(has_text="Classic Rock like Journey")).to_be_visible()
        expect(passenger.locator(".rt-axis-context").filter(has_text="Like Journey")).to_be_visible()


def test_track_sources_distinguish_blend_on_desktop_and_mobile(live_road_trip_app, page, monkeypatch):
    base_url, store = live_road_trip_app
    songs = [{"key": f"own-{i}", "video_id": f"own-{i}", "title": f"Library song {i}",
              "artist": f"Library artist {i}", "album": "", "thumbnail": None, "duration": 300,
              "genre": "Rock" if i % 2 else "Jazz", "year": 1995, "liked": False} for i in range(12)]
    monkeypatch.setattr(store, "library_songs", lambda: songs)
    rid = store.save_road_trip_recipe(None, "Three pools", 50, [], ["Rock"], 60, 1000)
    state = road_trip_rec.start_draft(store, store.get_road_trip_recipe(rid), 1000, seed=7)
    state["pool"] = [road_trip_rec._candidate(
        f"their-{i}", f"Passenger song {i}", f"Passenger artist {i}", "", None, 300,
        "theirs", "Pop", 1995) for i in range(12)]
    for candidate in state["pool"]:
        candidate["score"] = 1
    road_trip_rec.finish_draft(state, store, 1000)
    # Reopening an old draft repairs the underfilled Blend allocation without requiring a shuffle.
    state["targets"]["mine"] = {"genre:Rock": .34, "genre:Jazz": .66}
    first_blend = next(c for c in state["picked"] if c["mix_source"] == "blend")
    state["picked"] = [c for c in state["picked"] if c["mix_source"] != "blend" or c is first_blend]
    state["picks"] = [c["video_id"] for c in state["picked"]]
    state.pop("mix_version")
    store.save_road_trip_draft(rid, state, 1000)
    page.goto(f"{base_url}/road_trip")
    expect(page.locator(".rt-pool-counts")).to_have_text("4 Yours · 4 Theirs · 4 Blend")
    expect(page.locator(".rt-balance-control output")).to_have_text("34% Yours · 33% Theirs · 33% Blend")
    for source in ("yours", "theirs", "blend"):
        badges = page.locator(f".rt-c-src.is-{source}")
        expect(badges).to_have_count(4)
        expect(badges.first).to_have_text(source.capitalize())
    page.set_viewport_size({"width": 390, "height": 844})
    for source in ("yours", "theirs", "blend"):
        row = page.locator(f'.rt-row[data-mix-source="{source}"]').first
        badge = row.locator(".rt-c-src")
        badge.scroll_into_view_if_needed()
        expect(badge).to_be_visible()
        row_box, badge_box = row.bounding_box(), badge.bounding_box()
        assert badge_box["x"] + badge_box["width"] <= row_box["x"] + row_box["width"]
        title_box = row.locator(".rt-c-title").bounding_box()
        assert title_box["x"] + title_box["width"] <= badge_box["x"]


@pytest.mark.parametrize("legacy_backend", [False, True])
def test_saved_overallocated_passenger_balance_is_normalized(live_road_trip_app, page, monkeypatch, legacy_backend):
    base_url, store = live_road_trip_app
    genres = ["Post-Punk", "Singer-Songwriter", "Indie Rock", "Alternative Rock"]
    rid = store.save_road_trip_recipe(None, "Saved balance", 0, [], genres, 60, 1000)
    state = road_trip_rec.start_draft(store, store.get_road_trip_recipe(rid), 1000, seed=1)
    state["pool"] = [road_trip_rec._candidate(
        f"track-{i}", f"Song {i}", f"Artist {i}", "", None, 300, "theirs", genre, 1995)
        for i, genre in enumerate(genres)]
    for candidate in state["pool"]:
        candidate["score"] = 1
    state["own_facts_left"] = 0
    road_trip_rec.finish_draft(state, store, 1000)
    # The saved values from the reported draft total 213%, before a slider is even touched.
    state["targets"]["theirs"] = {f"genre:{g}": p for g, p in zip(genres, [1, .25, .53, .35])}
    for row in state["axes"]["theirs"]:
        row["target"] = state["targets"]["theirs"].get(row["key"])
        if legacy_backend:
            row.pop("balance_pct", None)
    store.save_road_trip_draft(rid, state, 1000)
    held = []
    monkeypatch.setattr(road_trip_route, "_spawn", held.append)
    if legacy_backend:
        monkeypatch.setattr(road_trip_rec, "normalized", lambda s, *args: s)
        page.route("**/tilt/v2", lambda route: route.fulfill(status=404, body="Not Found"))
    page.goto(f"{base_url}/road_trip")
    passenger = page.locator(".rt-step").filter(has=page.locator("summary strong", has_text="Passenger music"))
    for genre, pct in zip(genres, [47, 12, 25, 16]):
        expect(passenger.get_by_role("slider", name=genre, exact=True)).to_have_value(str(pct))

    if legacy_backend:
        passenger.get_by_role("slider", name="Post-Punk", exact=True).press("End")
        expect(passenger.get_by_text("Restart TuneConsole to load the updated mix controls.")).to_be_visible()
        for genre, pct in zip(genres, [47, 12, 25, 16]):
            expect(passenger.get_by_role("slider", name=genre, exact=True)).to_have_value(str(pct))
        assert store.get_road_trip_draft(rid)["targets"]["theirs"] == state["targets"]["theirs"]
        return

    try:
        old_form = page.locator(".rt-form").element_handle()
        passenger.get_by_role("slider", name="Post-Punk", exact=True).press("End")
        page.wait_for_function("form => !form.isConnected", arg=old_form)
        for genre, pct in zip(genres, [100, 0, 0, 0]):
            expect(passenger.get_by_role("slider", name=genre, exact=True)).to_have_value(str(pct))
        assert sum(store.get_road_trip_draft(rid)["targets"]["theirs"].values()) == 1
    finally:
        for worker in held:
            worker()

    expect(passenger.get_by_role("slider", name="Post-Punk", exact=True)).to_be_enabled(timeout=10000)
    # A background enrichment can discover another genre after the targets were set. Its observed
    # share must not be added on top of the existing 100% requested budget on the next render.
    saved = store.get_road_trip_draft(rid)
    saved["axes"]["theirs"].append({"key": "genre:Funk", "kind": "genre", "name": "Funk",
                                     "share": .4, "target": None})
    store.save_road_trip_draft(rid, saved, 1000)
    page.reload()
    for genre, pct in zip(genres + ["Funk"], [100, 0, 0, 0, 0]):
        expect(passenger.get_by_role("slider", name=genre, exact=True)).to_have_value(str(pct))
