"""The first mix arrives through local polling while the initial library sync is unfinished."""
from playwright.sync_api import expect
import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.library.sync import sync_identity


@pytest.fixture
def live_initial_sync(monkeypatch, request):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.jobs import SyncJobs

    jobs = SyncJobs()
    job = jobs.create(kind="library")
    job.events.append({"type": "info", "text": "Reading your playlists"})
    monkeypatch.setattr(webapp, "SyncJobs", lambda: jobs)
    return request.getfixturevalue("live_app"), job


@pytest.mark.parametrize('width,already_ready', [(320, False), (1280, False), (1280, True)])
def test_album_first_onboarding_preserves_player_and_opens_ready_albums(live_app, store, page, width, already_ready):
    from tests.onboarding_support import publish_personal_album

    page.set_viewport_size({'width': width, 'height': 1000})
    page.route('**/bridge/status', lambda route: route.fulfill(json={
        'connected': True, 'now_playing': {'video_id': 'listening', 'title': 'Still playing',
                                          'artist': 'Artist', 'paused': False}}))
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    errors, navigations = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(live_app)
    page.on('framenavigated', lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)
    expect(page.get_by_role('heading', name='How do you like to listen?')).to_be_visible()
    expect(page.locator('.listening-format-option')).to_have_count(3)
    expect(page.locator('.global-now-playing')).to_contain_text('Still playing')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('.listening-format-options').evaluate('''el => Promise.all(
        el.getAnimations({subtree: true}).map(animation => animation.finished))''')
    page.screenshot(path=f'/tmp/tuneconsole-format-picker-{width}.png', full_page=True)
    page.evaluate('window.firstDocument = document; window.firstPlayer = document.querySelector(".global-now-playing")')

    def finish_import():
        store.settings.set_setting('last_sync_at', '1')
        publish_personal_album(store, now=1)

    if already_ready:
        finish_import()
        # A completed shared status poll must not discard an unanswered picker.
        page.evaluate('''async () => {
            const data = await (await fetch('/home/onboard/status')).json();
            window.setupStatus.snapshot = data;
            window.dispatchEvent(new CustomEvent('setup-status', {detail: data}));
        }''')
        expect(page.locator('#listening-format-title')).to_be_visible()
    page.get_by_role('button', name='Start with Long play').click()
    if not already_ready:
        expect(page.locator('.arrival-intro')).to_contain_text('next favorite records')
        expect(page.locator('#listening-format-title')).to_have_count(0)
        finish_import()
    expect(page.get_by_role('tab', name='Long play', exact=True)).to_have_attribute('aria-selected', 'true', timeout=6500)
    expect(page.locator('.album-choice')).to_have_count(1)
    expect(page.locator('.album-choice-title')).to_contain_text('Your next record')
    expect(page.locator('#onboarding-feed')).to_have_count(0)
    expect(page.locator('main.page-setup')).to_have_count(0)
    assert page.evaluate('firstDocument === document && firstPlayer === document.querySelector(".global-now-playing")')
    assert store.settings.get_setting('onboard_listening_format') == 'albums'
    assert store.rec_proposals.get_proposals('home_prepared') is None
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.goto(live_app)
    expect(page.get_by_role('tab', name='Long play', exact=True)).to_have_attribute('aria-selected', 'true')
    assert navigations == [live_app + '/']
    assert errors == []


def test_successful_initial_sync_preserves_document_player_and_details(live_initial_sync, store, page):
    from tests.conftest import seed_catalog_track
    from tests.onboarding_support import publish_personal_mix

    base, job = live_initial_sync
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True, "now_playing": {"video_id": "listening", "title": "Still playing",
            "artist": "Artist", "paused": False},
    }))
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    errors, navigations = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(base)
    page.on("framenavigated", lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)
    page.get_by_role('button', name='Start with Mix tapes').click()
    expect(page.locator('#arrival-title')).to_be_visible()
    expect(page.locator("#onboarding-feed")).to_be_visible()
    player = page.locator(".global-now-playing")
    expect(player).to_be_visible()
    page.wait_for_function("""() => {
        const home = Alpine.$data(document.querySelector('#home'));
        return home.running && home._job !== null;
    }""", timeout=6500)
    page.locator(".setup-banner").get_by_role("button", name="Details").click()
    details = page.get_by_role("dialog")
    expect(details).to_be_visible()
    expect(details.get_by_text("Reading your playlists", exact=True)).to_be_visible()
    page.evaluate("""() => {
        window.importState = {
            document, home: document.querySelector('#home'),
            player: document.querySelector('.global-now-playing'),
            playback: window.playbackStatus,
            dialog: document.querySelector('.setup-details'),
        };
    }""")

    # Finish the real, already-attached SSE job while the user reads its details.
    seed_catalog_track(store, "listening|artist", "Still playing", "Artist",
                       video_id="listening", genre="house")
    store.settings.set_setting("last_sync_at", "1")
    job.events.append({"type": "done", "text": "Your playlists are ready"})
    job.outcome = "complete"
    job.done = True
    page.wait_for_function("""() => {
        const home = Alpine.$data(document.querySelector('#home'));
        return !home.running && !home.failed;
    }""")
    # Stay on this screen beyond the old 900 ms document-reload timer.
    page.wait_for_timeout(1200)
    expect(details.get_by_text("Your playlists are ready", exact=True)).to_be_visible(timeout=6500)
    expect(page.locator(".setup-evidence")).to_contain_text("1 song ready", timeout=6500)
    assert page.evaluate("""() => importState.document === document
        && importState.home === document.querySelector('#home')
        && importState.player === document.querySelector('.global-now-playing')
        && importState.playback === window.playbackStatus
        && importState.dialog === document.querySelector('.setup-details')
        && importState.dialog.open""")
    expect(page.locator("#onboarding-feed")).to_be_visible()

    # A sparse account's usable offer graduates Home before any mode can be fitted.
    assert store.modes.list_modes(active_only=True) == []
    publish_personal_mix(store, now=1)
    assert store.modes.list_modes(active_only=True) == []
    status = page.request.get(f"{base}/home/onboard/status")
    assert status.ok
    arrival = status.json()
    assert arrival["songs"] == 5, arrival
    assert arrival["setup"]["modes"] == 0, arrival
    assert arrival["setup"]["mixes_ready"], arrival
    assert arrival["setup"]["complete"], arrival
    expect(page.locator("#home-feed")).to_be_attached(timeout=6500)
    expect(page.locator("#onboarding-feed")).to_have_count(0)
    expect(page.locator(".setup-banner")).to_be_hidden()
    expect(page.locator(".setup-evidence")).to_contain_text("Ready")
    expect(details).to_be_visible()
    expect(player).to_contain_text("Still playing")
    assert page.evaluate("""() => importState.document === document
        && importState.home !== document.querySelector('#home')
        && importState.player === document.querySelector('.global-now-playing')
        && importState.playback === window.playbackStatus
        && importState.dialog === document.querySelector('.setup-details')
        && importState.dialog.open""")
    assert navigations == []
    assert errors == []


@pytest.mark.parametrize("width", [390, 1280])
def test_first_mix_arrives_before_full_sync(live_app, store, page, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True, "now_playing": None, "radio": False,
        "sensor_health": {"healthy": True, "ytm_tabs": 1},
    }))
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(live_app)
    page.get_by_role('button', name='Start with Mix tapes').click()
    expect(page.locator("#arrival-title")).to_have_text("Getting to know your music")
    expect(page.locator("#arrival-title")).to_be_visible()
    expect(page.locator("#takeout-nag")).to_have_count(0)
    expect(page.locator("#onboarding-start")).to_contain_text("Finding something to listen to")
    page.locator(".setup-banner").get_by_role("button", name="Details").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    page.keyboard.press("Escape")
    assert page.locator("#arrival-title").evaluate("el => getComputedStyle(el).textAlign") == "left"
    assert "Hanken Grotesk" in page.locator("#arrival-title").evaluate("el => getComputedStyle(el).fontFamily")
    expect(page.locator(".arrival .ui-arrow, .arrival-eyebrow")).to_have_count(0)
    expect(page.locator(".arrival-illustration")).to_be_visible()
    assert page.locator(".arrival-illustration").evaluate("el => el.complete && el.naturalWidth > 1000")

    # An incomplete playlist can supply catalog evidence, but cannot seed this mix.
    store.tracks.observe_track_entry("partial", "Unverified song", "Artist", now=1)
    iid = store.identities.get_identities()[0].id
    fake = FakeClient(playlists=[{"playlistId": "PL1", "title": "Ready", "count": 1}],
                      tracks={"PL1": [_track("ready", "First playable song", "Artist")]})
    sync_identity(store, iid, fake, now=1)
    assert store.settings.get_setting("last_sync_at") is None

    card = page.locator("#gen-onboard_library")
    expect(card).to_be_visible(timeout=6500)
    expect(card.locator(".gen-title")).to_have_text("First playable song")
    expect(card.get_by_role("button", name="Save and play this mix")).to_be_visible()
    expect(page.locator("#arrival-title")).to_have_text("Getting to know your music")
    expect(card).to_contain_text("Something to listen to")
    expect(card).to_contain_text("while we complete setup for you.")
    expect(page.locator(".arrival-counts, .arrival-features, #library-import-details")).to_have_count(0)
    expect(page.locator(".arrival")).not_to_contain_text("Welcome")
    expect(page.locator("#onboarding-start")).not_to_have_attribute("hx-trigger", "every 3s")
    assert "Unverified song" not in card.inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    expect(card.get_by_role("button")).to_have_count(1)
    tracks = page.evaluate("JSON.parse(genTracks('gen-onboard_library'))")
    assert [track["video_id"] for track in tracks] == ["ready"]
    page.locator(".setup-banner").get_by_role("button", name="Details").click()
    expect(page.locator(".setup-evidence")).to_contain_text("1 song ready", timeout=6500)
    page.keyboard.press("Escape")
    page.locator(".starter-mix").evaluate("el => Promise.all(el.getAnimations({subtree: true}).map(animation => animation.finished))")
    page.screenshot(path=f"/tmp/tuneconsole-onboarding-ready-{width}.png", full_page=True)
    assert errors == []


@pytest.mark.parametrize("width", [390, 1280])
def test_setup_banner_follows_navigation_and_finishes_when_models_are_ready(live_app, store, page, width):
    import numpy as np
    from tests.conftest import seed_catalog_track

    seed_catalog_track(store, "ready|artist", "Ready to play", "Artist", video_id="ready", genre="house")
    store.settings.set_setting("last_sync_at", "1")
    page.set_viewport_size({"width": width, "height": 1000})
    page.route("**/bridge/status", lambda route: route.fulfill(json={"connected": True}))
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(live_app)
    banner = page.locator(".setup-banner")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("Preparing your personal mixes")
    expect(page.locator("#arrival-title")).to_have_text("Getting to know your music")
    expect(page.locator(".arrival-features, .arrival-counts, #library-import-details")).to_have_count(0)
    expect(page.locator(".starter-context")).to_have_text("while we complete setup for you.")
    banner.get_by_role("button", name="Details").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    expect(page.locator(".setup-evidence")).to_contain_text("1 song ready")
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator(".arrival-copy").evaluate("el => Promise.all(el.getAnimations().map(animation => animation.finished))")
    page.locator(".starter-mix").evaluate("el => Promise.all(el.getAnimations({subtree: true}).map(animation => animation.finished))")
    page.screenshot(path=f"/tmp/tuneconsole-onboarding-learning-{width}.png", full_page=True)

    page.goto(live_app + "/playlists")
    expect(banner).to_be_visible()
    banner.get_by_role("button", name="Details").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    store.modes.replace_modes([{"mode_id": 1, "label": "House", "families": [["house", 1]],
        "centroid": np.array([1.0], dtype=np.float32), "size": 5, "rep_keys": []}], [], 1)
    page.keyboard.press("Escape")
    # Imported likes and fitted modes still cannot replace the first experience
    # with an empty Mix tapes page, including ordinary Home navigation and Back.
    store.settings.set_setting("onboard_dismissed", "1")
    for i in range(30):
        store.rec_model.record_like(f"imported {i}|artist", now=1)
    page.goto(live_app)
    expect(page.locator("#onboarding-feed")).to_be_visible()
    expect(page.locator(".setup-banner")).to_be_visible()
    page.goto(live_app + "/playlists")
    page.go_back()
    expect(page.locator("#onboarding-feed")).to_be_visible()
    banner.get_by_role("button", name="Details").click()
    from tests.onboarding_support import publish_personal_mix
    publish_personal_mix(store, now=1)
    expect(banner).to_be_hidden(timeout=6500)
    # Finishing setup does not take away the details a user is currently reading.
    expect(page.get_by_role("dialog")).to_be_visible()
    expect(page.locator(".setup-evidence")).to_contain_text("Ready")
    expect(page.locator("#home-feed")).to_be_attached(timeout=6500)
    expect(page.locator("#onboarding-feed")).to_have_count(0)
    page.keyboard.press("Escape")
    page.reload()
    expect(page.locator(".setup-banner")).to_have_count(0)
    assert errors == []


def test_extension_setup_and_live_import_details(live_app, page):
    connected = {"connected": False, "now_playing": None, "radio": False}
    page.route("**/bridge/status", lambda route: route.fulfill(json=connected))
    status = {"songs": 0, "covers": [], "playlists": 0, "albums": 0, "outcome": None,
              "setup": {"complete": False, "imported": False, "running": False,
                        "analyzed": 0, "total": 0, "mixes_ready": False}}
    log = {"events": [], "outcome": None}
    page.route("**/home/onboard/status", lambda route: route.fulfill(json=status))
    page.route("**/sync/recent", lambda route: route.fulfill(json=log))
    page.goto(live_app)
    page.get_by_role('button', name='Start with Mix tapes').click()
    install = page.get_by_role("link", name="Get the extension")
    expect(install).to_be_visible()
    expect(page.get_by_role("link", name="YouTube Music", exact=True)).to_be_visible()
    page.locator("main").evaluate("el => Promise.all(el.getAnimations({subtree: true}).filter(a => a.effect.getComputedTiming().iterations !== Infinity).map(a => a.finished))")
    page.screenshot(path="/tmp/tuneconsole-onboarding-connect.png", full_page=True)
    page.locator(".setup-banner").get_by_role("button", name="Details").click()
    details = page.get_by_role("dialog")
    expect(details).to_be_visible()
    expect(details).to_contain_text("Connect the extension to get started")

    connected["connected"] = True
    status["setup"].update(running=True, activity="playlists", current=2, target=5)
    log["events"].append({"type": "info", "text": "Reading playlist 2 of 5: Confessionals"})
    page.evaluate("window.dispatchEvent(new CustomEvent('bridge-status', {detail: {connected: true}}))")
    expect(install).to_be_hidden()
    expect(page.locator(".setup-banner-step")).to_have_text("Reading playlist 2 of 5", timeout=6500)
    expect(details.get_by_text("Reading playlist 2 of 5: Confessionals")).to_be_visible(timeout=6500)
    status["setup"]["running"] = False
    status["outcome"] = "partial"
    log["events"].append({"type": "err", "text": "Library partially updated."})
    expect(page.locator(".setup-banner-step")).to_have_text("Keep YouTube Music open", timeout=6500)
    expect(details.get_by_text("Library partially updated.", exact=True)).to_be_visible()


@pytest.mark.parametrize("width", [320, 1440])
def test_temporary_listening_layout_holds_a_full_queue(live_app, store, page, width):
    from tests.conftest import seed_catalog_track

    songs = [("Angel", "Massive Attack"), ("Dayvan Cowboy", "Boards of Canada"),
             ("Everything in Its Right Place", "Radiohead"), ("Glory Box", "Portishead"),
             ("Hyperballad", "Björk"), ("Teardrop", "Massive Attack"),
             ("All My Friends", "LCD Soundsystem"), ("Roads", "Portishead"),
             ("Roygbiv", "Boards of Canada"), ("Svefn-g-englar", "Sigur Rós"),
             ("A long song title that should stay inside its own column", "A long artist name"),
             ("Oblivion", "Grimes")]
    for i, (title, artist) in enumerate(songs):
        seed_catalog_track(store, f"sample {i}|artist", title, artist, video_id=f"sample-{i}")
    store.settings.set_setting("last_sync_at", "1")
    page.set_viewport_size({"width": width, "height": 1000})
    page.route("**/bridge/status", lambda route: route.fulfill(json={"connected": True}))
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    page.goto(live_app)
    expect(page.locator(".starter-track")).to_have_count(12)
    expect(page.locator(".starter-context")).to_have_text("while we complete setup for you.")
    page.locator("main").evaluate("el => Promise.all(el.getAnimations({subtree: true}).filter(a => a.effect.getComputedTiming().iterations !== Infinity).map(a => a.finished))")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator(".starter-track").first.bounding_box()["height"] < 100
    page.screenshot(path=f"/tmp/tuneconsole-onboarding-full-{width}.png", full_page=True)
