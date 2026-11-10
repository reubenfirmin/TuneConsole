"""Live-behavior test for the Road Trip tab: create a recipe through the real form, build it into
the on-screen playlist, curate that playlist (cross a slot out, move a slider), and only then save
it - confirming the resulting playlist is tagged Generated (taste-model quarantine + GC eligibility)
and that nothing reached YouTube before the save."""
import re
import json
import os
from pathlib import Path
import socket
import threading
import time
from urllib.parse import parse_qs
from uuid import uuid4

import pytest
import uvicorn

from tests.live_server import LiveServer
from playwright.sync_api import expect

from yt_playlist.rec import road_trip as road_trip_rec
from yt_playlist.rec import road_trip_candidates as candidates
from yt_playlist.rec.road_trip_axes import canonical_genre
from yt_playlist.repos.base import GENERATED_GROUP
from yt_playlist.web.app import create_app
from yt_playlist.web.routes import road_trip as road_trip_route
from tests.conftest import FakeClient, _track
from tests.browser_deadline import JourneyTimeout, browser_diagnostics, supervise_journey
from tests.browser.quiet_action_support import action_evidence, assert_quiet_action

pytestmark = pytest.mark.browser


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def live_road_trip_app(monkeypatch, unlocked_home_controls, store):
    store.genres.configure_catalog()
    iid = store.accounts.upsert_account("Main", "cred", None, True)
    # A handful of songs of your own, not one: your side of the mix is the whole collection, so a
    # one-song library leaves nothing to swap in when a slot is crossed out.
    pid = store.playlists.upsert_playlist(iid, "PL1", "Mix", 6, "h", 1.0)
    mine = [store.tracks.upsert_track(f"v{i}", f"My Song {i}", f"My Artist {i}", "Alb", 200, 1)
            for i in range(6)]
    store.playlists.set_playlist_tracks(pid, mine)
    # The server runs in this process, so patching the Deezer facts lookup here keeps the build
    # (which enriches every "theirs" candidate) entirely offline.
    monkeypatch.setattr(candidates, "_facts",
                        lambda title, artist: {"popularity": 500, "year": 2015,
                                               "genre": "psychedelic", "duration": 245})
    monkeypatch.setattr(candidates, "artist_genre", lambda s, name: "Psychedelic Rock")
    # Picking an artist auto-fills their GENRE, and a genre input resolves through Last.fm and
    # MusicBrainz to find that genre's top artists. Stub it: without this the build makes real
    # network calls, which is both wrong for a test and slow enough to miss the timeout.
    monkeypatch.setattr(candidates, "genre_artists",
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
    # A successful provider write must be verifiable by reading back every song.
    client._catalog.update({f'v{i}': _track(f'v{i}', f'My Song {i}', f'My Artist {i}')
                            for i in range(6)})
    client._catalog.update({row['videoId']: row for row in client._artists['UC1']['songs']['results']})
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 1000.0)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    with LiveServer(server):
        yield f"http://127.0.0.1:{port}", store


def _journey_report(request):
    return Path(os.environ.get('TUNECONSOLE_BROWSER_JOURNEY_REPORT',
        str(Path(request.config.getoption('output')) / 'road-trip-diagnostics' / request.node.name)))






@pytest.fixture
def paused_road_trip_builds(monkeypatch):
    workers = []
    monkeypatch.setattr(road_trip_route, '_spawn', workers.append)
    yield workers
    for worker in workers:
        worker.release()






@pytest.mark.parametrize('edit', ['slider'])
def test_progress_preserves_queued_and_uncommitted_edits_then_resumes(
        live_road_trip_app, paused_road_trip_builds, page, edit):
    base_url, store = live_road_trip_app
    rid = store.road_trip.save_road_trip_recipe(None, 'Pending edits', 50, [], ['Rock'], 60, 1000)
    page.set_default_timeout(10000)
    pending = []

    def progress(route):
        response = route.fetch()
        pending.append((route, response.body()))

    page.route('**/road_trip/draft/*/progress', progress)
    page.goto(f'{base_url}/road_trip')
    page.evaluate("""() => {
      window.realTimeout = window.setTimeout; window.queuedSteers = [];
      window.setTimeout = (fn, ms, ...args) => {
        if (ms === 350) {
          queuedSteers.push(() => fn(...args)); return realTimeout(() => {}, 3600000);
        }
        if (ms === 900) return realTimeout(() => {}, 3600000);
        return realTimeout(fn, ms, ...args);
      };
    }""")
    page.locator('.rt-saved-routes summary').click()
    page.locator('.rt-gen-btn').click()
    expect(page.locator('.rt-draft.is-building')).to_be_visible()
    form = page.locator('.rt-form').element_handle()

    def request_progress():
        count = len(pending)
        with page.expect_request(f'**/road_trip/draft/{rid}/progress'):
            page.evaluate("""rid => { htmx.ajax('GET', '/road_trip/draft/' + rid + '/progress',
              {target: '#road-trip-body', swap: 'innerHTML'}); }""", rid)
        # The request event precedes the route's response body, especially when
        # a current-owner snapshot must be opened. Wait for that exact capture.
        deadline = time.monotonic() + 10
        while len(pending) == count and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        assert len(pending) == count + 1
        return pending[-1]

    poll, old_body = request_progress()
    if edit in ('duration', 'slider'):
        mix = page.locator('.rt-step').filter(has=page.locator('summary strong', has_text='Shape the mix'))
        if not mix.evaluate('el => el.open'):
            mix.locator('summary').click()
    if edit == 'genre':
        page.locator('.rt-step summary').filter(has_text='Passenger music').click()
        genre = page.get_by_role('combobox', name='Add a passenger genre', exact=True)
        genre.fill('Funk')
        page.wait_for_function('Array.isArray(window.__genreOpts)')
        # The full taxonomy has more than eight subgenres containing "funk".
        # Its exact match must still be offered inside the bounded suggestion list.
        expect(page.locator('.rt-select-menu:visible .rt-suggestion').first.locator('span')).to_have_text('funk')
        page.locator('.rt-suggestion').get_by_text('funk', exact=True).click()
    elif edit == 'duration':
        page.get_by_role('button', name='30 min', exact=True).click()
    elif edit == 'slider':
        page.locator('#rt-familiarity').evaluate("""el => {
          el.focus(); el.value = '80'; el.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
    else:
        page.get_by_role('button', name='Rename', exact=True).click()
        page.get_by_role('textbox', name='Route name', exact=True).fill('An unfinished new name')
    poll.fulfill(body=old_body, content_type='text/html')
    page.wait_for_timeout(50)
    assert page.evaluate('form => form.isConnected', form)
    assert store.road_trip.get_road_trip_recipe(rid)['name'] == 'Pending edits'

    # A second old poll must also stay obsolete after the save's new editor is mounted.
    stale_poll, stale_body = request_progress()
    if edit == 'name':
        page.get_by_role('button', name='Save name', exact=True).click()
    else:
        if edit == 'slider':
            page.locator('#rt-familiarity').dispatch_event('change')
        page.evaluate("""() => {
          window.setTimeout = realTimeout;
          queuedSteers.splice(0).forEach(run => run());
        }""")
    page.wait_for_function('form => !form.isConnected', arg=form)
    saved_form = page.locator('.rt-form').element_handle()
    stale_poll.fulfill(body=stale_body, content_type='text/html')
    page.wait_for_timeout(50)
    assert page.evaluate('form => form.isConnected', saved_form)
    recipe = store.road_trip.get_road_trip_recipe(rid)
    if edit == 'genre':
        assert 'Funk' in {canonical_genre(g) for g in recipe['genres']}
    elif edit == 'duration':
        assert recipe['target_minutes'] == 30
    elif edit == 'slider':
        assert recipe['familiarity_pct'] == 80
    else:
        assert recipe['name'] == 'An unfinished new name'
    fresh_poll, fresh_body = request_progress()
    fresh_poll.fulfill(body=fresh_body, content_type='text/html')
    page.wait_for_function('form => !form.isConnected', arg=saved_form)


def test_build_curate_then_save_road_trip_recipe(request, browser_name):
    report = _journey_report(request)
    if not supervise_journey(request, browser_name, report):
        return
    # Fixtures, every interaction, and teardown belong to the supervised
    # worker. The parent never opens the worker's Playwright transport.
    live_app = request.getfixturevalue('live_road_trip_app')
    page = request.getfixturevalue('page')
    monkeypatch = request.getfixturevalue('monkeypatch')
    with browser_diagnostics(page, report) as checkpoint:
        _build_curate_then_save_road_trip_recipe(live_app, page, monkeypatch, checkpoint)


def _build_curate_then_save_road_trip_recipe(live_road_trip_app, page, monkeypatch, road_trip_journey):
    base_url, store = live_road_trip_app
    road_trip_journey('open recipe editor')
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
    road_trip_journey('save recipe')
    page.click(".rt-form button:has-text('Save recipe')")
    expect(page.locator(".rt-recipe")).to_contain_text("Beach Run")

    # Build: the playlist appears on the page right away (your half first), their tracks stream in,
    # and the controls arrive once it settles. Nothing is on YouTube yet.
    road_trip_journey('build and stream draft tracks')
    page.click(".rt-recipe button:has-text('Build playlist')")
    expect(page.locator(".rt-draft")).to_be_visible(timeout=10000)
    expect(page.locator(".rt-list .rt-row").first).to_be_visible()
    expect(page.locator(".rt-draft button:has-text('Save to YouTube')")).to_be_visible(timeout=10000)
    assert store.road_trip.list_road_trip_recipes()[0]["last_playlist_id"] is None
    rid = store.road_trip.list_road_trip_recipes()[0]["id"]
    seed = store.road_trip.get_road_trip_draft(rid)['seed']
    expect(page.locator(".rt-axes .fp-slider").first).to_be_visible()

    # Draft songs open the same global menu as library tracks, while Swap remains immediate.
    road_trip_journey('open draft song actions')
    track = page.locator(".rt-list .rt-row").first
    track.get_by_title("More actions").click()
    expect(page.locator("#song-action-menu")).to_have_count(1)
    page.get_by_role("menuitem", name="Create playlist from song").click()
    expect(page.get_by_label("Playlist name")).to_have_value(track.locator(".rt-c-title").inner_text())
    page.get_by_role("dialog").get_by_role("button", name="Cancel").click()

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
        road_trip_journey('replace recipe form after ' + selector)
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
        assert store.road_trip.get_road_trip_recipe(rid)[field] == expected
        draft = store.road_trip.get_road_trip_draft(rid)
        assert draft[field] == expected and draft['seed'] == seed

    page.get_by_role("button", name="Choose passenger music").click()
    expect(passenger).to_have_js_property("open", True)
    expect(mix).to_have_js_property("open", False)
    expect(passenger.locator(".rt-axis-context").first).to_contain_text("Tame Impala")

    # Genre and year sliders share the recipe form. Moving one must send its own value,
    # without the hidden sliders in another section overriding it.
    road_trip_journey('pin passenger genre to zero')
    genre_slider = passenger.locator(".fp-slider").first
    old_form = page.locator(".rt-form").element_handle()
    with page.expect_request(f"**/road_trip/draft/{rid}/tilt/v2") as request:
        genre_slider.press("Home")
    posted = parse_qs(request.value.post_data)
    assert posted["share"] == ["0"]
    page.wait_for_function("form => !form.isConnected", arg=old_form)
    expect(genre_slider).to_have_value("0")
    expect(passenger).to_have_js_property("open", True)
    state = store.road_trip.get_road_trip_draft(rid)
    assert state["targets"]["theirs"][posted["axis"][0]] == 0.0
    assert state["targets"]["mine"] == {}
    assert state["stats"]["their_count"] == 0

    # Releasing that pin restores the passenger tracks before continuing to curate the mix.
    old_form = page.locator(".rt-form").element_handle()
    road_trip_journey('restore passenger genre balance')
    passenger.get_by_role("button", name="Reset genre balance").click()
    page.wait_for_function("form => !form.isConnected", arg=old_form)
    expect(genre_slider).to_have_value("100")
    assert store.road_trip.get_road_trip_draft(rid)["targets"]["theirs"] == {}

    passenger.locator("summary").press("Enter")
    expect(page.locator(".rt-step[open]")).to_have_count(0)
    mix.locator("summary").press("Enter")
    expect(mix).to_have_js_property("open", True)

    # Cross a slot out: the recipe fills it back in rather than leaving a hole. Assert on the row's
    # video id, which expect() retries until the swap lands (a title read races the swap).
    road_trip_journey('swap a draft slot')
    rows = page.locator(".rt-list .rt-row")
    before = rows.count()
    first_vid = rows.first.get_attribute("data-vid")
    rows.first.locator(".rt-x").click()
    expect(page.locator(".rt-list .rt-row").first).not_to_have_attribute("data-vid", first_vid)
    expect(page.locator(".rt-list .rt-row")).to_have_count(before)
    expect(mix).to_have_js_property("open", True)
    expected_videos = rows.evaluate_all("rows => rows.map(row => row.dataset.vid)")

    saving, release = threading.Event(), threading.Event()
    create_playlist = road_trip_route.playlist_generated.create_generated_playlist
    clients = []

    def slow_save(*args, **kwargs):
        assert [track['video_id'] for track in args[2]] == expected_videos
        clients.append(args[3])
        saving.set()
        assert release.wait(15), "test did not release the YouTube save"
        return create_playlist(*args, **kwargs)

    monkeypatch.setattr(road_trip_route.playlist_generated, "create_generated_playlist", slow_save)
    try:
        road_trip_journey('open loading popup while provider save is held')
        with page.expect_popup() as opened:
            page.get_by_role("button", name="Save to YouTube").click()
        loading = opened.value
        expect(loading).to_have_url(re.compile(r'^blob:' + re.escape(base_url) + '/'))
        expect(loading.get_by_role("heading", name="Building on YouTube Music", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Save to YouTube")).to_be_disabled()
        assert saving.wait(5), 'The save request did not reach the provider boundary'
        assert store.road_trip.get_road_trip_recipe(rid)["last_playlist_id"] is None
    finally:
        release.set()
    road_trip_journey('finish remote save and navigate to Generated playlist')
    page.wait_for_url("**/playlist/*", timeout=10000)
    if not loading.is_closed():
        loading.wait_for_event('close', timeout=5000)
    assert loading.is_closed()

    recipes = store.road_trip.list_road_trip_recipes()
    assert len(recipes) == 1
    last_ytm = recipes[0]["last_playlist_id"]
    assert last_ytm is not None
    assert store.playlists.get_playlist_groups()[last_ytm] == GENERATED_GROUP
    playlist = next(p for p in store.playlists.get_playlists() if p.provider_playlist_id == last_ytm)
    expect(page).to_have_url(f"{base_url}/playlist/{playlist.id}")
    songs = store.playlists.get_playlist_tracks_with_meta(playlist.id)
    assert [row[1] for row in songs] == expected_videos
    assert len(clients) == 1 and len(clients[0].created) == 1
    assert clients[0].created[0][:2] == (last_ytm, 'Road Trip: Beach Run #1')
    assert [video for pid, videos in clients[0].added if pid == last_ytm
            for video in videos] == expected_videos
    assert [track['videoId'] for track in clients[0].get_playlist(last_ytm, limit=None)['tracks']] == expected_videos
    assert any(row[1].startswith('vt') for row in songs), 'The saved mix must contain passenger music'
    assert all(row[2].strip() and row[3].strip() for row in songs), songs
    assert all((row[2], row[3]) == (f'Their Song {row[1][2:]}', 'Tame Impala')
               for row in songs if row[1].startswith('vt')), songs
    road_trip_journey('verify saved passenger identities and rendered labels')
    for _, _, title, artist, *_ in songs:
        expect(page.get_by_text(title, exact=True).first).to_be_visible()
        expect(page.get_by_text(artist, exact=True).first).to_be_visible()
    page.screenshot(path='/tmp/tuneconsole-ready-715-save-labels.png', full_page=True)


def test_failed_save_closes_loading_screen_and_keeps_the_draft(live_road_trip_app, page, monkeypatch):
    base_url, store = live_road_trip_app
    rid = store.road_trip.save_road_trip_recipe(None, "Save failure", 100, [], [], 20, 1000)
    state = road_trip_rec.start_draft(store, store.road_trip.get_road_trip_recipe(rid), 1000, seed=7)
    road_trip_rec.finish_draft(state, store, 1000)
    state['revision'] = uuid4().hex
    store.road_trip.save_road_trip_draft(rid, state, 1000)
    release = threading.Event()

    def failed_save(*args, **kwargs):
        assert release.wait(15), "test did not release the YouTube save"
        raise RuntimeError("YouTube unavailable")

    monkeypatch.setattr(road_trip_route.playlist_generated, "create_generated_playlist", failed_save)
    page.goto(f"{base_url}/road_trip")
    try:
        with page.expect_popup() as opened:
            page.get_by_role("button", name="Save to YouTube").click()
        loading = opened.value
        expect(loading.get_by_role("heading", name="Building on YouTube Music", exact=True)).to_be_visible()
    finally:
        release.set()
    expect(page.locator(".gen-done")).to_contain_text("Couldn't save")
    expect(page.get_by_role("button", name="Save to YouTube")).to_be_enabled()
    assert loading.is_closed()
    expect(page).to_have_url(f"{base_url}/road_trip")
    expect(page.locator(".rt-row")).to_have_count(len(state["picked"]))
    assert store.road_trip.get_road_trip_draft(rid)["saved_playlist_id"] is None




@pytest.mark.parametrize("addition", ["artist"])
def test_adding_passenger_music_updates_sliders_before_tracks_arrive(live_road_trip_app, page, monkeypatch, addition):
    base_url, store = live_road_trip_app
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    genres = ["Rock", "Pop", "Jazz"]
    rid = store.road_trip.save_road_trip_recipe(None, "Add music", 0, [], genres, 100, 1000)
    state = road_trip_rec.start_draft(store, store.road_trip.get_road_trip_recipe(rid), 1000, seed=7)
    state["pool"] = [candidates._candidate(
        f"{genre}-{i}", f"Song {i}", f"Artist {genre} {i}", "", None, 300, "theirs", genre, 1995)
        for genre in genres for i in range(12)]
    for candidate in state["pool"]:
        candidate["score"] = 1
    state["targets"]["theirs"] = {"genre:" + g: p for g, p in zip(genres, [.6, .3, .1])}
    state["done_inputs"] = genres.copy()
    state["own_facts_left"] = 0
    road_trip_rec.finish_draft(state, store, 1000)
    state['revision'] = uuid4().hex
    store.road_trip.save_road_trip_draft(rid, state, 1000)
    held = []
    monkeypatch.setattr(road_trip_route, "_spawn", held.append)
    monkeypatch.setattr(candidates, "artist_genre",
                        lambda store, name: "Classic Rock" if name == "Journey" else None)
    page.route("**/road_trip/autocomplete/artists?*",
               lambda route: route.fulfill(json={"results": ["Journey"]}))

    def songs(client, kind, name, cap, store=None, decade=None):
        genre = "Classic Rock" if kind == "artist" else name
        return ([{"video_id": f"new-{name}-{i}", "title": f"New song {i}",
                  "artist": name if kind == "artist" else f"{name} Artist {i}", "album": "",
                  "thumbnail": None, "duration": 300, "genre": genre} for i in range(8)], [])

    monkeypatch.setattr(candidates, "other_input_songs", songs)
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
        waiting = store.road_trip.get_road_trip_draft(rid)
        assert waiting["building"]
        assert not any(c["genre"] == new_genre for c in waiting["pool"])
        assert new_genre in {canonical_genre(g) for g in store.road_trip.get_road_trip_recipe(rid)["genres"]}
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
    assert any(c["genre"] == new_genre for c in store.road_trip.get_road_trip_draft(rid)["picked"])
    page.reload()
    for genre, pct in zip(genres + [new_genre], [45, 23, 7, 25]):
        expect(passenger.get_by_role("slider", name=genre, exact=True)).to_have_value(str(pct))
    if addition == "artist":
        expect(passenger.locator(".genre-chip.is-theirs").filter(has_text="Classic Rock like Journey")).to_be_visible()
        expect(passenger.locator(".rt-axis-context").filter(has_text="Like Journey")).to_be_visible()




@pytest.mark.parametrize("legacy_backend", [True])
def test_saved_overallocated_passenger_balance_is_normalized(live_road_trip_app, page, monkeypatch, legacy_backend):
    base_url, store = live_road_trip_app
    genres = ["Post-Punk", "Singer-Songwriter", "Indie Rock", "Alternative Rock"]
    rid = store.road_trip.save_road_trip_recipe(None, "Saved balance", 0, [], genres, 60, 1000)
    state = road_trip_rec.start_draft(store, store.road_trip.get_road_trip_recipe(rid), 1000, seed=1)
    state["pool"] = [candidates._candidate(
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
    state['revision'] = uuid4().hex
    store.road_trip.save_road_trip_draft(rid, state, 1000)
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
        expect(passenger.get_by_text("The balance could not be confirmed. Check your route before trying again.")).to_be_visible()
        for genre, pct in zip(genres, [47, 12, 25, 16]):
            expect(passenger.get_by_role("slider", name=genre, exact=True)).to_have_value(str(pct))
        assert store.road_trip.get_road_trip_draft(rid)["targets"]["theirs"] == state["targets"]["theirs"]
        return

    try:
        old_form = page.locator(".rt-form").element_handle()
        passenger.get_by_role("slider", name="Post-Punk", exact=True).press("End")
        page.wait_for_function("form => !form.isConnected", arg=old_form)
        for genre, pct in zip(genres, [100, 0, 0, 0]):
            expect(passenger.get_by_role("slider", name=genre, exact=True)).to_have_value(str(pct))
        assert sum(store.road_trip.get_road_trip_draft(rid)["targets"]["theirs"].values()) == 1
    finally:
        for worker in held:
            worker()

    expect(passenger.get_by_role("slider", name="Post-Punk", exact=True)).to_be_enabled(timeout=10000)
    # A background enrichment can discover another genre after the targets were set. Its observed
    # share must not be added on top of the existing 100% requested budget on the next render.
    saved = store.road_trip.get_road_trip_draft(rid)
    saved["axes"]["theirs"].append({"key": "genre:Funk", "kind": "genre", "name": "Funk",
                                     "share": .4, "target": None})
    store.road_trip.save_road_trip_draft(rid, saved, 1000)
    page.reload()
    for genre, pct in zip(genres + ["Funk"], [100, 0, 0, 0, 0]):
        expect(passenger.get_by_role("slider", name=genre, exact=True)).to_have_value(str(pct))
