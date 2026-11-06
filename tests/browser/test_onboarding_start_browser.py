"""The first mix arrives through local polling while the initial library sync is unfinished."""
import json
from tests.derived_rating_support import seed_like
import re

from playwright.sync_api import expect
import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.library.sync import sync_identity


@pytest.mark.parametrize('width', [1440])
def test_partial_sync_opens_ready_mixes_without_reloading_or_waiting_for_enrichment(live_app, store, page, width):
    from tests.onboarding_support import finish_partial_import, publish_personal_mix

    finish_partial_import(store, now=1)
    page.route('**/bridge/status', lambda route: route.fulfill(json={
        'connected': True, 'now_playing': {'video_id': 'still-playing', 'title': 'Still playing',
                                          'artist': 'Artist', 'paused': False}}))
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app)
    expect(page.locator('#onboarding-feed')).to_be_visible()
    expect(page.locator('.global-now-playing')).to_contain_text('Still playing')
    page.evaluate('window.arrivalDocument = document; window.arrivalPlayer = document.querySelector(".global-now-playing")')
    from tests.test_growth_journey import early_journey
    early_journey(store)
    publish_personal_mix(store, now=1)
    expect(page.locator('#onboarding-feed')).to_have_count(0, timeout=12000)
    expect(page.get_by_role('tab', name='Mix tapes', exact=True)).to_be_visible()
    expect(page.locator('#home-mode-cards .gen-card')).to_have_count(4)
    expect(page.locator('.setup-banner')).to_be_hidden()
    expect(page.locator('.global-now-playing')).to_contain_text('Still playing')
    assert page.evaluate('arrivalDocument === document && arrivalPlayer === document.querySelector(".global-now-playing")')
    assert store.settings.get_setting('last_sync_at') is None
    assert store.settings.get_setting('last_sync_outcome') == 'partial'
    assert store.enrichment_reports.coverage_stats()['processed'] == 0
    page.reload()
    expect(page.get_by_role('tab', name='Mix tapes', exact=True)).to_be_visible()
    expect(page.locator('#onboarding-feed')).to_have_count(0)
    assert page.locator('body').evaluate('body => body.scrollWidth <= innerWidth')
    page.screenshot(path=f'/tmp/tuneconsole-559-ready-partial-{width}.png', full_page=True)
    assert not errors


@pytest.mark.parametrize('width', [1440])
def test_completion_fragment_recovers_after_one_failed_fetch(live_app, store, page, width):
    from tests.onboarding_support import finish_partial_import, publish_personal_album

    finish_partial_import(store, now=1)
    page.route('**/bridge/status', lambda route: route.fulfill(json={
        'connected': True, 'now_playing': {'video_id': 'playing', 'title': 'Still playing',
                                         'artist': 'Artist', 'paused': False}}))
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app)
    expect(page.locator('#onboarding-feed')).to_be_visible()
    page.evaluate('window.recoveryDocument = document; window.recoveryPlayer = document.querySelector(".global-now-playing")')
    attempts = []

    def completion(route):
        attempts.append(route.request.method)
        if len(attempts) == 1:
            route.fulfill(status=503, body='Temporarily unavailable')
        else:
            route.continue_()

    page.route(live_app + '/', completion)
    from tests.test_growth_readiness import fresh_reserve, prepared_taste
    fresh_reserve(store)
    prepared_taste(store)
    publish_personal_album(store, now=1, count=2)
    try:
        expect(page.locator('#onboarding-feed')).to_have_count(0, timeout=15000)
    except AssertionError:
        state = page.evaluate('''() => {
            const root = document.querySelector('#onboarding-feed');
            const state = root && Alpine.$data(root);
            return {snapshot: window.setupStatus?.snapshot, opening: state?._openingHome,
                disposed: state?._disposed, retry: state?._homeRetry, delay: state?._homeRetryDelay};
        }''')
        pytest.fail(f'Home completion did not recover: {attempts=}, {state=}, {errors=}')
    expect(page.get_by_role('tab', name='Long play', exact=True)).to_be_visible()
    assert attempts == ['GET', 'GET']
    assert page.evaluate('recoveryDocument === document && recoveryPlayer === document.querySelector(".global-now-playing")')
    expect(page.locator('.global-now-playing')).to_contain_text('Still playing')
    assert store.settings.get_setting('last_sync_at') is None
    assert store.settings.get_setting('last_sync_outcome') == 'partial'
    assert store.enrichment_reports.coverage_stats()['processed'] == 0
    assert not errors


def test_returning_home_recovers_a_late_mix_without_restarting_setup(live_app, store, page, tmp_path):
    from tests.conftest import seed_catalog_track
    from tests.onboarding_support import publish_personal_mix
    from yt_playlist.rec import home_cards, home_prepared
    from tests.home_fixture_support import returning_listener

    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('home_format', 'mixes')
    returning_listener(store, 'mixes')
    seed_catalog_track(store, 'listening|artist', 'Still playing', 'Artist',
                       video_id='listening', genre='house')
    page.route('**/bridge/status', lambda route: route.fulfill(json={
        'connected': True, 'now_playing': {'video_id': 'listening', 'title': 'Still playing',
                                          'artist': 'Artist', 'paused': False}}))
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.clock.install()
    page.goto(f'{live_app}/?prepare_attempt=10#directions')
    board = page.locator('#home-mode-cards')
    expect(board).to_have_attribute('hx-trigger', 'every 30s')
    expect(page.locator('#onboarding-feed')).to_have_count(0)
    expect(page.locator('.setup-banner')).to_have_count(0)
    expect(page.locator('.global-now-playing')).to_contain_text('Still playing')
    expect(page.get_by_role('tab', name='Mix tapes', exact=True)).to_have_attribute('aria-selected', 'true')
    page.evaluate('window.startupDocument = document; window.startupPlayer = document.querySelector(".global-now-playing")')
    page.clock.run_for(2000)

    # Preparation is still pending after several minutes and beyond the old
    # ten-request cutoff. The browser keeps its recovery timer attached.
    with page.expect_response('**/home/cards?prepare_attempt=10'):
        page.clock.fast_forward(180_000)
    # Response delivery precedes HTMX's asynchronous swap/settle timers. Advance
    # them before the next poll so its replacement element owns the timer.
    page.clock.run_for(2000)
    expect(board).to_have_attribute('hx-trigger', 'every 30s')
    comfort_tracks = publish_personal_mix(store, now=1, count=1)
    with page.expect_response('**/home/cards?prepare_attempt=10'):
        page.clock.fast_forward(30_000)
    expect(board.locator('.gen-card')).to_have_count(1)
    expect(board.locator('.gen-row')).to_have_count(4)
    expect(page.get_by_role('tab', name='Mix tapes', exact=True)).to_be_visible()
    expect(page.locator('.mix-preparation-note')).to_be_hidden()
    # One usable playlist dismisses the preparation note while the incomplete
    # board keeps polling for its remaining lanes.
    expect(board).to_have_attribute('hx-get', '/home/cards?prepare_attempt=10')
    expect(board).to_have_attribute('hx-trigger', 'every 30s')
    page.clock.run_for(2000)

    # Publish a complete worker-style board through the real cache boundary.
    # Fresh candidates remain outside the library; the other lanes are owned.
    protos = []
    for lane in ('wheelhouse', 'explore', 'fresh', 'comfort'):
        tracks = comfort_tracks if lane == 'comfort' else []
        if lane != 'comfort':
            for i in range(4):
                title, video_id = f'Recovery {lane} {i}', f'recovery-{lane}-{i}'
                key = f'{title.lower()}|artist'
                if lane == 'fresh':
                    key = store.discovery.upsert_discovered_track(
                        key, video_id, title, 'Artist', '', None, 'house', None, None, 1)
                else:
                    seed_catalog_track(store, key, title, 'Artist', video_id=video_id, genre='house')
                tracks.append({'key': key, 'title': title, 'artist': 'Artist',
                               'video_id': video_id, 'album': '', 'thumbnail': None, 'genre': 'house'})
        proto = home_cards._proto(store, lane, f'Recovered {lane}', tracks, 1)
        proto['recipe'] = {'lane': lane}
        protos.append(proto)
    result = home_cards.CardsResult(
        protos, [{'proto': proto, 'pool': None} for proto in protos],
        None, None, [], [], home_cards._epoch(store, 'cards'), None)
    home_prepared.remember(store, store.rec_proposals.get_proposals('home_prepared'), result, 1)
    with page.expect_response('**/home/cards?prepare_attempt=10'):
        page.clock.fast_forward(30_000)
    expect(board.locator('.gen-card')).to_have_count(4)
    expect(board.locator('.gen-row')).to_have_count(16)
    expect(page.locator('#home-mode-cards[hx-get]')).to_have_count(0)
    expect(page.locator('#home-mode-cards[hx-trigger]')).to_have_count(0)
    expect(page.locator('#onboarding-feed')).to_have_count(0)
    expect(page.locator('.global-now-playing')).to_contain_text('Still playing')
    assert page.evaluate('startupDocument === document && startupPlayer === document.querySelector(".global-now-playing")')
    with page.expect_response('**/home/growth'):
        page.clock.fast_forward(15_000)
    page.get_by_role('tab', name='Mix tapes', exact=True).click()
    # Settle HTMX's JavaScript timers, then await the actual CSS timeline. The
    # simulated clock does not establish that browser animations have finished.
    page.clock.run_for(2000)
    page.locator('main').evaluate('''el => Promise.all(
        el.getAnimations({subtree: true})
          .filter(animation => animation.effect.getComputedTiming().iterations !== Infinity)
          .map(animation => animation.finished))''')
    expect(board).to_be_visible()
    expect(board.locator('.gen-card').first).to_be_visible()
    page.screenshot(path=str(tmp_path / 'recovered-startup-mix.png'), full_page=True)
    assert board.locator('.gen-card').first.evaluate(
        "el => getComputedStyle(el).opacity === '1'")
    assert errors == []


@pytest.fixture
def live_initial_sync(monkeypatch, request):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.jobs import SyncJobs

    jobs = SyncJobs()
    job = jobs.create(kind="library")
    job.events.append({"type": "info", "text": "Reading your playlists"})
    monkeypatch.setattr(webapp, "SyncJobs", lambda: jobs)
    return request.getfixturevalue("live_app"), job


@pytest.mark.parametrize('width,already_ready', [(1280, False), (1280, True)])
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
    expect(page.locator('#arrival-title')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=f'/tmp/tuneconsole-welcome-{width}.png', full_page=True)
    expect(page.locator('.global-now-playing')).to_contain_text('Still playing')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.evaluate('window.firstDocument = document; window.firstPlayer = document.querySelector(".global-now-playing")')

    def finish_import():
        store.settings.set_setting('last_sync_at', '1')
        from tests.test_growth_readiness import fresh_reserve, prepared_taste
        fresh_reserve(store)
        prepared_taste(store)
        publish_personal_album(store, now=1, count=2)

    if already_ready:
        finish_import()
        # Shared status can reveal a playable room without asking for a preference.
        page.evaluate('''async () => {
            const data = await (await fetch('/home/onboard/status')).json();
            window.setupStatus.snapshot = data;
            window.dispatchEvent(new CustomEvent('setup-status', {detail: data}));
        }''')
    if not already_ready:
        finish_import()
    expect(page.get_by_role('tab', name='Long play', exact=True)).to_have_attribute('aria-selected', 'true', timeout=6500)
    expect(page.locator('.album-choice')).to_have_count(2)
    expect(page.locator('.album-choice-title').first).to_contain_text('Your next record')
    expect(page.locator('#onboarding-feed')).to_have_count(0)
    expect(page.locator('main.page-setup')).to_have_count(0)
    assert page.evaluate('firstDocument === document && firstPlayer === document.querySelector(".global-now-playing")')
    assert store.settings.get_setting('onboard_listening_format') is None
    assert store.settings.get_setting('home_format') is None
    assert store.rec_proposals.get_proposals('home_prepared')['choice_context']
    from yt_playlist.rec import onboarding
    assert not onboarding.prepared_mixes_ready(store, 1)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.goto(live_app)
    expect(page.get_by_role('tab', name='Long play', exact=True)).to_have_attribute('aria-selected', 'true')
    expect(page.locator('.album-choice')).to_have_count(2)
    page.locator('.album-choice').first.scroll_into_view_if_needed()
    page.evaluate('''async () => {
        await Promise.all(document.getAnimations().filter(animation =>
            Number.isFinite(animation.effect.getComputedTiming().iterations)
        ).map(animation => animation.finished.catch(() => {})));
    }''')
    page.screenshot(path=f'/tmp/tuneconsole-731-ready-albums-{already_ready}.png', full_page=True)
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
    from tests.test_growth_journey import early_journey
    early_journey(store)
    publish_personal_mix(store, now=1)
    assert store.modes.list_modes(active_only=True) == []
    status = page.request.get(f"{base}/home/onboard/status")
    assert status.ok
    arrival = status.json()
    assert arrival["songs"] == 18, arrival  # two existing songs and sixteen distinct menu songs
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


@pytest.mark.parametrize("width", [1280])
def test_first_mix_arrives_before_full_sync(live_app, store, page, width, tmp_path):
    page.set_viewport_size({"width": width, "height": 900})
    page.route("**/bridge/status", lambda route: route.fulfill(json={
        "connected": True, "now_playing": None, "radio": False,
        "sensor_health": {"healthy": True, "ytm_tabs": 1},
    }))
    page.route("https://i.ytimg.com/**", lambda route: route.abort())
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(live_app)
    expect(page.locator("#arrival-title")).to_have_text("Welcome to TuneConsole")
    expect(page.locator("#arrival-title")).to_be_visible()
    expect(page.locator("#takeout-nag")).to_have_count(0)
    expect(page.locator("#onboarding-start")).to_contain_text("Finding something to listen to")
    page.locator(".setup-banner").get_by_role("button", name="Details").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    page.keyboard.press("Escape")
    assert page.locator("#arrival-title").evaluate("el => getComputedStyle(el).textAlign") == "left"
    assert "Hanken Grotesk" in page.locator("#arrival-title").evaluate("el => getComputedStyle(el).fontFamily")
    expect(page.locator('.festival-intro .festival-map')).to_be_visible()
    starting_tent = page.locator('.festival-destination[aria-label="Go to my first set"]')
    first_art = page.locator('.festival-intro .landmark-art.is-soundcheck')
    expect(starting_tent).to_be_visible()
    expect(starting_tent).to_have_class(re.compile('is-untouched'))
    assert first_art.locator('.landmark-halo').evaluate('el => getComputedStyle(el).animationName') == 'none'
    assert first_art.locator('.landmark-halo').evaluate('el => getComputedStyle(el).opacity') == '0'
    assert starting_tent.locator('.landmark-hit').evaluate('el => el.getBoundingClientRect().bottom < innerHeight')
    expect(page.locator('.festival-entry-label')).to_have_count(0)
    expect(page.locator('.festival-destination.is-untouched:not([aria-label="Go to my first set"]) .landmark-hint:visible')).to_have_count(0)
    expect(page.locator('.account-health')).to_have_count(0)
    expect(page.locator('.arrival-illustration')).to_have_count(0)
    if width < 650:
        hint = page.locator('.festival-swipe-hint')
        expect(hint).to_be_visible()
        assert hint.evaluate('el => el.getBoundingClientRect().left >= 0 && el.getBoundingClientRect().right <= innerWidth')
    page.get_by_role('button', name='Mix tapes, Not started', exact=True).click()
    preview = page.get_by_role('dialog', name='Mix tapes', exact=True)
    expect(preview).to_be_visible()
    expect(preview.get_by_role('button', name='Open Mix tapes', exact=True)).not_to_be_visible()
    page.keyboard.press('Escape')
    if width < 650:
        expect(page.locator('.festival-swipe-hint')).to_be_hidden()
    progress = page.locator(".arrival-progress")
    expect(progress).to_be_visible()
    expect(progress).to_contain_text("Bringing in your music")
    assert progress.bounding_box()['y'] >= page.locator('.festival-map').bounding_box()['y']
    assert progress.locator(".arrival-wave > span").first.evaluate("""el =>
        getComputedStyle(el).animationName === 'presync-eq'
        && getComputedStyle(el).animationPlayState === 'running'
        && getComputedStyle(el).animationIterationCount === 'infinite'""")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator('main').evaluate('el => Promise.allSettled(el.getAnimations({subtree: true}).filter(a => a.effect.getComputedTiming().iterations !== Infinity).map(a => a.finished))')
    page.screenshot(path=str(tmp_path / f"onboarding-preparing-{width}.png"), full_page=True)
    page.screenshot(path=f'/tmp/tuneconsole-551-soundcheck-{width}.png', full_page=True)
    page.evaluate('window.soundcheckDocument = document; window.soundcheckPlayer = document.querySelector(".global-now-playing")')

    # An incomplete playlist can supply catalog evidence, but cannot seed this mix.
    store.tracks.observe_track_entry("partial", "Unverified song", "Artist", now=1)
    iid = store.accounts.get_accounts()[0].id
    fake = FakeClient(playlists=[{"playlistId": "PL1", "title": "Ready", "count": 1}],
                      tracks={"PL1": [_track("ready", "First playable song", "Artist")]})
    sync_identity(store, iid, fake, now=1)
    assert store.settings.get_setting("last_sync_at") is None

    card = page.locator("#gen-onboard_library")
    expect(card).to_be_visible(timeout=6500)
    expect(progress).to_be_hidden()
    expect(card.locator(".gen-title")).to_have_text("First playable song")
    expect(card.get_by_role("button", name="Save and play this mix")).to_be_visible()
    page.locator('.home-growth').evaluate('el => Alpine.$data(el).refresh()')
    expect(starting_tent).to_have_class(re.compile('is-ready'))
    assert first_art.locator('.landmark-halo').evaluate('el => getComputedStyle(el).animationName') == 'festival-soundcheck'
    expect(page.locator('.festival-intro-sub')).to_have_text('Your first set is ready')
    page.locator('.festival-first-set').click()
    expect(card.get_by_role('button', name='Save and play this mix')).to_be_focused()
    starting_tent.click()
    expect(card.get_by_role('button', name='Save and play this mix')).to_be_focused()
    page.locator('.topbar nav').get_by_role('link', name='Home', exact=True).click()
    expect(card.get_by_role('button', name='Save and play this mix')).to_be_focused()
    assert page.evaluate('soundcheckDocument === document && soundcheckPlayer === document.querySelector(".global-now-playing")')
    expect(card).to_contain_text("Something to listen to")
    expect(card).to_contain_text("while we complete setup for you.")
    expect(page.locator(".arrival-counts, .arrival-features, #library-import-details")).to_have_count(0)
    expect(page.locator('#arrival-title')).to_have_text('Welcome to TuneConsole')
    expect(page.locator("#onboarding-start")).not_to_have_attribute("hx-trigger", "every 3s")
    assert "Unverified song" not in card.inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    expect(card.get_by_role("button")).to_have_count(1)
    tracks = page.evaluate("JSON.parse(genTracks('gen-onboard_library'))")
    assert [track["video_id"] for track in tracks] == ["ready"]
    page.locator(".setup-banner").get_by_role("button", name="Details").click()
    expect(page.locator(".setup-evidence")).to_contain_text("1 song ready", timeout=6500)
    page.keyboard.press("Escape")
    page.locator(".starter-mix").evaluate("el => Promise.all(el.getAnimations({subtree: true}).filter(animation => animation.effect.getComputedTiming().iterations !== Infinity).map(animation => animation.finished))")
    page.screenshot(path=f"/tmp/tuneconsole-onboarding-ready-{width}.png", full_page=True)
    # Hearing the same upload in another playlist still does not mean the
    # offered first set was started. The First sounds milestone stays explored.
    store.history.record_play_event(iid, store.tracks.identity_key_for_video('ready'), 'ready', 2,
                                    playlist_ytm_id='UNRELATED')
    page.locator('.topbar nav').get_by_role('link', name='Home', exact=True).click()
    expect(page.locator('.festival-intro-sub')).to_have_text('Your first set is ready')
    page.reload()
    expect(page.locator('.festival-intro-sub')).to_have_text('Your first set is ready')
    assert errors == []


@pytest.mark.parametrize('width,height', [(1440, 720), (1920, 1080)])
def test_welcome_starting_tent_is_visible_above_the_fold_and_player(live_app, store, page, width, height):
    from tests.conftest import seed_catalog_track

    seed_catalog_track(store, 'ready|artist', 'Ready to play', 'Artist', video_id='ready')
    store.settings.set_setting('last_sync_at', '1')
    page.route('**/bridge/status', lambda route: route.fulfill(json={
        'connected': True, 'now_playing': {'video_id': 'ready', 'title': 'Ready to play',
                                          'artist': 'Artist', 'paused': False}}))
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.set_viewport_size({'width': width, 'height': height})
    page.goto(live_app)
    expect(page.locator('.festival-intro-sub')).to_be_visible()
    expect(page.locator('.festival-intro-sub')).to_have_text('Your first set is ready')
    expect(page.locator('.global-now-playing')).to_contain_text('Ready to play')
    page.evaluate('''async () => {
        await document.fonts.ready;
        await Promise.allSettled(document.getAnimations().filter(a =>
            a.effect.getComputedTiming().iterations !== Infinity).map(a => a.finished));
        await new Promise(requestAnimationFrame);
    }''')
    for selector in ('.global-now-playing .hs-playpause', '.festival-first-set'):
        control = page.locator(selector)
        expect(control).to_be_in_viewport(ratio=1)
        assert control.evaluate('''el => {
            const bounds = el.getBoundingClientRect();
            return el.contains(document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2));
        }''')
    expect(page.locator('.festival-destination[aria-label="Go to my first set"] .landmark-hit')).to_be_in_viewport(ratio=1)
    if width >= 1440:
        assert page.locator('#arrival-title').evaluate('''title =>
            title.getBoundingClientRect().height <= parseFloat(getComputedStyle(title).lineHeight) + 1''')
    page.screenshot(path=f'/tmp/tuneconsole-564-welcome-fold-{width}.png')
    assert page.locator('.festival-destination[aria-label="Go to my first set"] .landmark-hit').evaluate('''tent => {
        const bounds = tent.getBoundingClientRect();
        const player = document.querySelector('.global-now-playing').getBoundingClientRect();
        const overlaps = bounds.left < player.right && bounds.right > player.left
            && bounds.top < player.bottom && bounds.bottom > player.top;
        return bounds.top >= 0 && bounds.bottom <= innerHeight && !overlaps;
    }''')
    # The scene begins at the top of the intro; welcome copy overlays its lake,
    # rather than reserving an opaque header above the artwork.
    assert page.locator('.festival-intro').evaluate('''intro => {
        const art = intro.querySelector('.festival-map').getBoundingClientRect();
        const frame = intro.getBoundingClientRect();
        const copy = intro.querySelector('.festival-intro-copy').getBoundingClientRect();
        const tent = intro.querySelector('.landmark-hit').getBoundingClientRect();
        return Math.abs(art.top - frame.top) < 1 && Math.abs(art.left - frame.left) < 1
            && copy.top >= art.top && copy.bottom < tent.top;
    }''')
    expect(page.locator('.festival-routes .growth-route:not(.is-explored):not(.is-ready):not(.is-preparing):visible')).to_have_count(0)
    frontier = page.locator('.festival-routes .growth-route.is-preparing')
    expect(frontier).to_have_count(1)
    expect(frontier).to_have_attribute('data-destination', 'foundation')
    expect(frontier).to_be_visible()
    assert frontier.evaluate('''route => {
        const style = getComputedStyle(route);
        return parseFloat(style.strokeWidth) >= 24 && parseFloat(style.opacity) < .6
            && style.strokeDasharray === 'none';
    }''')


def test_welcome_player_access_survives_home_return_resize_and_disconnect(live_app, store, page):
    from pathlib import Path
    from tests.conftest import seed_catalog_track

    seed_catalog_track(store, 'ready|artist', 'Ready to play', 'Artist', video_id='ready')
    store.settings.set_setting('last_sync_at', '1')
    bridge = {'connected': True, 'now_playing': {'video_id': 'ready', 'title': 'Ready to play',
        'artist': 'Artist', 'paused': False,
        'source': {'kind': 'playlist', 'url': '/playlist/1', 'title': 'My playlist'}}}
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/bridge/status', lambda route: route.fulfill(json=bridge))
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.set_viewport_size({'width': 1280, 'height': 720})
    page.goto(live_app)
    expect(page.locator('.festival-first-set')).to_be_visible()
    page.evaluate('window.originalWelcomePlayer = document.querySelector(".global-now-playing")')

    def receives_pointer(selector):
        control = page.locator(selector)
        expect(control).to_be_in_viewport(ratio=1)
        assert control.evaluate('''el => {
            const rect = el.getBoundingClientRect();
            return el.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
        }''')

    for width, height in ((1280, 720), (1440, 820), (1920, 1080), (1280, 720)):
        page.set_viewport_size({'width': width, 'height': height})
        page.evaluate('''async () => {
            await document.fonts.ready;
            await Promise.allSettled(document.getAnimations().filter(a =>
                a.effect.getComputedTiming().iterations !== Infinity).map(a => a.finished));
            await new Promise(requestAnimationFrame);
        }''')
        receives_pointer('.global-now-playing .hs-playpause')
        receives_pointer('.festival-first-set')
        expect(page.locator('.festival-intro .landmark-hit').first).to_be_in_viewport(ratio=1)
        page.screenshot(path=f'/tmp/tuneconsole-612-player-source-{width}.png')
    page.locator('.festival-first-set').click()
    expect(page.locator('[data-onboarding-play]')).to_be_focused()
    receives_pointer('.global-now-playing .hs-playpause')
    page.locator('.topbar nav').get_by_role('link', name='Home', exact=True).click()
    expect(page.locator('[data-onboarding-play]')).to_be_focused()
    receives_pointer('.global-now-playing .hs-playpause')
    assert page.evaluate('originalWelcomePlayer === document.querySelector(".global-now-playing")')

    bridge['connected'] = False
    page.evaluate('window.playbackStatus.refresh()')
    expect(page.locator('.global-now-playing')).to_be_hidden()
    page.evaluate("scrollTo({top: 0, behavior: 'instant'})")
    expect(page.locator('.arrival-progress')).to_contain_text('Open YouTube Music')
    expect(page.locator('.festival-first-set')).to_be_hidden()
    bridge.update(connected=True, now_playing=None)
    page.evaluate('window.playbackStatus.refresh()')
    expect(page.locator('.festival-first-set')).to_be_visible()
    expect(page.locator('.global-now-playing')).to_be_hidden()
    page.evaluate("scrollTo({top: 0, behavior: 'instant'})")
    page.wait_for_function('scrollY === 0')
    page.evaluate('() => new Promise(requestAnimationFrame)')
    receives_pointer('.festival-first-set')
    for asset in ('home.js', 'player-card.js', 'account-growth.css'):
        response = page.request.get(live_app + '/static/' + asset)
        assert response.status == 200
        assert response.body() == (Path(__file__).resolve().parents[2] /
            'src/yt_playlist/web/static' / asset).read_bytes()
    assert not errors


@pytest.mark.parametrize("width", [1280])
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
    assert banner.bounding_box()['y'] == 0
    expect(page.locator('.account-health, #account-health-slot')).to_have_count(0)
    expect(page.locator("#arrival-title")).to_have_text("Welcome to TuneConsole")
    expect(page.locator('.festival-intro-sub')).to_have_text('Your first set is ready')
    expect(page.locator(".arrival-features, .arrival-counts, #library-import-details")).to_have_count(0)
    expect(page.locator(".starter-context")).to_have_text("while we complete setup for you.")
    banner.get_by_role("button", name="Details").click()
    expect(page.get_by_role("dialog")).to_be_visible()
    expect(page.locator(".setup-evidence")).to_contain_text("1 song ready")
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator('.festival-intro').evaluate('el => Promise.all(el.getAnimations({subtree: true}).filter(a => a.effect.getComputedTiming().iterations !== Infinity).map(a => a.finished))')
    page.locator(".starter-mix").evaluate("el => Promise.all(el.getAnimations({subtree: true}).filter(animation => animation.effect.getComputedTiming().iterations !== Infinity).map(animation => animation.finished))")
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
        seed_like(store, f"imported {i}|artist", now=1)
    page.goto(live_app)
    expect(page.locator("#onboarding-feed")).to_be_visible()
    expect(page.locator(".setup-banner")).to_be_visible()
    page.goto(live_app + "/playlists")
    page.go_back()
    expect(page.locator("#onboarding-feed")).to_be_visible()
    banner.get_by_role("button", name="Details").click()
    from tests.onboarding_support import publish_personal_mix
    from tests.test_growth_journey import early_journey
    early_journey(store)
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
    expect(page.locator('.account-health, #account-health-slot')).to_have_count(0)
    assert errors == []


@pytest.mark.parametrize('width', [1280])
def test_first_album_is_playable_before_sync_and_cues_only_its_player(live_app, store, page, width, tmp_path):
    from urllib.parse import parse_qs

    store.settings.set_setting('onboard_listening_format', 'albums')
    store.collection.add_saved_album({'browse': 'MPRE_starter', 'title': 'The first record',
                                      'artist': 'An artist', 'year': '2001'})
    status = {'connected': True, 'now_playing': {'video_id': 'other', 'title': 'Other song',
        'artist': 'Another artist', 'paused': False, 'playlist_ytm_id': 'OTHER'}}
    page.set_viewport_size({'width': width, 'height': 1000})
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    page.route('**/album/play', lambda route: route.fulfill(status=204))
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(live_app)
    card = page.locator('.starter-album')
    expect(card).to_be_visible()
    byline = page.locator('.festival-intro-sub')
    expect(byline).to_have_text('Your first set is ready')
    expect(card).to_contain_text('The first record')
    expect(card.locator('.starter-context')).to_have_text(
        "Here's an album to listen to while we perform initial customization.")
    expect(page.locator('.starter-mix')).to_have_count(0)
    expect(page.locator('.arrival-progress')).to_be_hidden()
    play = card.get_by_role('button', name='Play The first record', exact=True)
    assert play.evaluate("el => getComputedStyle(el, '::after').animationName") == 'onboarding-orbit, onboarding-breathe'
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    card.evaluate('''el => Promise.all(el.getAnimations({subtree:true})
        .filter(a => a.effect.getComputedTiming().iterations !== Infinity).map(a => a.finished))''')
    page.screenshot(path=str(tmp_path / f'onboarding-first-album-{width}.png'), full_page=True)
    player = page.locator('.global-now-playing')
    with page.expect_request('**/album/play') as request:
        play.click()
    assert parse_qs(request.value.post_data)['browse_id'] == ['MPRE_starter']
    assert page.evaluate("JSON.parse(sessionStorage.getItem('tc-onboarding-playback')).album") == 'MPRE_starter'
    expect(player).not_to_have_class(re.compile('is-onboarding-cue'))
    # A song with album metadata in an unrelated playlist cannot satisfy the handoff.
    status['now_playing'].update(title='An album song', album_browse_id='MPRE_starter',
        source={'kind': 'playlist', 'url': '/playlist/1'})
    page.evaluate('window.playbackStatus.refresh()')
    expect(player).not_to_have_class(re.compile('is-onboarding-cue'))
    expect(byline).to_have_text('Your first set is ready')
    status['now_playing'].update(playlist_ytm_id='ALBUM_QUEUE', paused=True,
        source={'kind': 'album', 'url': '/album?browse=MPRE_starter', 'title': 'The first record'})
    page.evaluate('window.playbackStatus.refresh()')
    expect(player).not_to_have_class(re.compile('is-onboarding-cue'))
    expect(byline).to_have_text('Your first set is ready')
    status['now_playing']['paused'] = False
    page.evaluate('window.playbackStatus.refresh()')
    expect(player).to_have_class(re.compile('is-onboarding-cue'))
    expect(byline).to_have_text('Your first set is ready')
    page.locator('.topbar nav').get_by_role('link', name='Home', exact=True).click()
    expect(byline).to_have_text('Enjoy your first set while we prepare your account')
    page.reload()
    expect(byline).to_be_visible()
    expect(byline).to_have_text('Enjoy your first set while we prepare your account')
    assert store.settings.get_setting('last_sync_at') is None
    assert errors == []


@pytest.mark.parametrize('player_field', ['now_playing', 'resumable_player'])
def test_starter_album_resume_waits_for_confirmed_playback(live_app, store, page, player_field, tmp_path):
    from urllib.parse import parse_qs, urlparse

    browse = 'MPRE_paused_starter'
    store.settings.set_setting('onboard_listening_format', 'albums')
    store.collection.add_saved_album({'browse': browse, 'title': 'The paused first set', 'artist': 'An artist'})
    song = {'video_id': 'album-song', 'title': 'An album song', 'artist': 'An artist',
        'paused': True, 'source': {'kind': 'album', 'url': '/album?browse=' + browse}}
    status = {'connected': True, 'now_playing': None, player_field: song}
    album_requests, errors = [], []
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    page.route('**/now-playing/toggle*', lambda route: route.fulfill(json={'ok': True}))
    page.route('**/album/play', lambda route: (album_requests.append(route.request.url),
        route.fulfill(status=503, body='Resume must use the existing toggle')))
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(live_app)
    byline = page.locator('.festival-intro-sub')
    expect(byline).to_have_text('Your first set is ready')
    assert page.evaluate("sessionStorage.getItem('tc-onboarding-played')") is None
    with page.expect_request('**/now-playing/toggle*') as request:
        page.get_by_role('button', name='Play The paused first set', exact=True).click()
    assert request.value.method == 'POST'
    if player_field == 'resumable_player':
        assert parse_qs(urlparse(request.value.url).query) == {
            'action': ['toggle'], 'resume': ['true'], 'videoId': ['album-song']}
    assert page.evaluate("JSON.parse(sessionStorage.getItem('tc-onboarding-playback')).album") == browse
    expect(byline).to_have_text('Your first set is ready')
    assert page.evaluate("sessionStorage.getItem('tc-onboarding-played')") is None

    # The accepted resume is only intent. Even the same item in a different
    # album, a paused matching album, or a disconnected bridge cannot confirm it.
    status['resumable_player'] = None
    status['now_playing'] = {**song, 'paused': False,
        'source': {'kind': 'album', 'url': '/album?browse=MPRE_other'}}
    page.evaluate('window.playbackStatus.refresh()')
    expect(byline).to_have_text('Your first set is ready')
    status['now_playing'].update(paused=True, source=song['source'])
    page.evaluate('window.playbackStatus.refresh()')
    expect(byline).to_have_text('Your first set is ready')
    status['connected'] = False
    status['now_playing']['paused'] = False
    page.evaluate('window.playbackStatus.refresh()')
    expect(byline).to_be_hidden()
    assert page.evaluate("sessionStorage.getItem('tc-onboarding-played')") is None
    expect(page.locator('.global-now-playing')).not_to_have_class(re.compile('is-onboarding-cue'))
    status['connected'] = True
    page.evaluate('window.playbackStatus.refresh()')
    expect(page.locator('.global-now-playing')).to_have_class(re.compile('is-onboarding-cue'))
    expect(byline).to_have_text('Your first set is ready')
    page.locator('.topbar nav').get_by_role('link', name='Home', exact=True).click()
    expect(byline).to_have_text('Enjoy your first set while we prepare your account')
    page.reload()
    expect(byline).to_have_text('Enjoy your first set while we prepare your account')
    page.screenshot(path=str(tmp_path / f'onboarding-album-resume-{player_field}-1280.png'), full_page=True)
    assert album_requests == []
    assert errors == []


def test_ready_radio_starter_uses_existing_session_controls(live_app, store, page):
    from tests.conftest import seed_catalog_track

    store.settings.set_setting('onboard_listening_format', 'radio')
    seed_catalog_track(store, 'radio seed|artist', 'First radio song', 'Artist', video_id='radio-seed')
    store.settings.set_setting('radio_seed_offer', json.dumps({'menu_id': 'starter-radio-offer',
        'tracks': [{'key': 'radio seed|artist', 'video_id': 'radio-seed',
                    'title': 'First radio song', 'artist': 'Artist'}]}))
    page.route('**/bridge/status', lambda route: route.fulfill(json={'connected': True, 'now_playing': None}))
    page.route('**/radio/status', lambda route: route.fulfill(json={'active': False, 'connected': True}))
    # Rendering an existing offer must not refit or replace its seed suggestions.
    seed_requests = []
    page.route('**/radio/seeds', lambda route: (seed_requests.append(route.request.url),
        route.fulfill(json={'tracks': []})))
    page.route('**/radio/start', lambda route: route.fulfill(json={'ok': False, 'reason': 'Controlled launch rejection'}))
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(live_app)
    expect(page.locator('.onboarding-radio')).to_be_visible()
    expect(page.locator('.starter-mix')).to_have_count(0)
    expect(page.locator('.arrival-progress')).to_be_hidden()
    with page.expect_request('**/radio/start') as request:
        page.get_by_role('button', name='Start radio with First radio song by Artist').click()
    assert request.value.post_data_json['video_id'] == 'radio-seed'
    assert request.value.post_data_json['menu_id'] == 'starter-radio-offer'
    recovery = page.get_by_role('status', name='Starting radio')
    expect(recovery).to_contain_text('First radio song')
    expect(recovery.get_by_role('button', name='Try this selection again')).to_be_enabled()
    with page.expect_request('**/radio/start') as retried:
        recovery.get_by_role('button', name='Try this selection again').click()
    initial, retry = request.value.post_data_json, retried.value.post_data_json
    assert initial['launch_id'] and retry['launch_id'] != initial['launch_id']
    assert {key: value for key, value in retry.items() if key != 'launch_id'} == {
        key: value for key, value in initial.items() if key != 'launch_id'}
    expect(recovery.get_by_role('button', name='Try this selection again')).to_be_enabled()
    recovery.scroll_into_view_if_needed()
    page.evaluate('''async () => {
        await Promise.all(document.getAnimations().filter(animation =>
            Number.isFinite(animation.effect.getComputedTiming().iterations)
        ).map(animation => animation.finished.catch(() => {})));
    }''')
    page.screenshot(path='/tmp/tuneconsole-731-radio-retry.png', full_page=True)
    expect(page.locator('.festival-intro-sub')).to_have_text('Your first set is ready')
    assert page.evaluate("sessionStorage.getItem('tc-onboarding-played')") is None
    assert seed_requests == []
    assert errors == []


@pytest.mark.parametrize('width', [1280])
def test_radio_first_set_byline_waits_for_its_confirmed_playback(live_app, store, page, width):
    from tests.conftest import seed_catalog_track

    store.settings.set_setting('onboard_listening_format', 'radio')
    seed_catalog_track(store, 'radio seed|artist', 'First radio song', 'Artist', video_id='radio-seed')
    store.settings.set_setting('radio_seed_offer', json.dumps({'menu_id': 'starter-radio-offer',
        'tracks': [{'key': 'radio seed|artist', 'video_id': 'radio-seed',
                    'title': 'First radio song', 'artist': 'Artist'}]}))
    bridge = {'connected': True, 'now_playing': {'video_id': 'other', 'title': 'Other music',
        'artist': 'Other artist', 'paused': False, 'playlist_ytm_id': 'OTHER',
        'source': {'kind': 'playlist', 'url': '/playlist/1'}}}
    state = {'active': False, 'connected': True}
    page.set_viewport_size({'width': width, 'height': 900})
    page.route('**/bridge/status', lambda route: route.fulfill(json=bridge))
    page.route('**/radio/status', lambda route: route.fulfill(json=state))
    page.route('**/radio/start', lambda route: route.fulfill(json={**state, 'ok': True}))
    page.route('**/radio/playlist*', lambda route: route.fulfill(body='<div>First radio song</div>'))
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(live_app)
    play = page.get_by_role('button', name='Start radio with First radio song by Artist')
    expect(play).to_be_visible()
    byline = page.locator('.festival-intro-sub')
    expect(byline).to_have_text('Your first set is ready')
    # Use the mounted component's real bootstrap shape; only the launch result
    # and bridge reports are controlled. A successful start is still not sound.
    initial = page.locator('.onboarding-radio .radio-home').evaluate('el => JSON.parse(JSON.stringify(Alpine.$data(el).state))')
    state.update({**initial, 'active': True, 'starting': True, 'playlist_id': 'FIRST_RADIO'})
    play.click()
    expect(page.locator('.onboarding-radio .radio-launch')).to_be_visible()
    page.wait_for_function("JSON.parse(sessionStorage.getItem('tc-onboarding-playback'))?.playlist === 'FIRST_RADIO'")
    assert page.evaluate("JSON.parse(sessionStorage.getItem('tc-onboarding-playback')).playlist") == 'FIRST_RADIO'
    expect(byline).to_have_text('Your first set is ready')
    bridge['now_playing'].update(video_id='radio-seed', title='First radio song', artist='Artist',
        playlist_ytm_id='FIRST_RADIO', paused=True, source={'kind': 'radio', 'url': '/#radio'})
    page.evaluate('window.playbackStatus.refresh()')
    expect(byline).to_have_text('Your first set is ready')
    bridge['now_playing'].update(paused=False, source={'kind': 'playlist', 'url': '/playlist/1'})
    page.evaluate('window.playbackStatus.refresh()')
    expect(byline).to_have_text('Your first set is ready')
    bridge['now_playing']['source'] = {'kind': 'radio', 'url': '/#radio'}
    page.evaluate('window.playbackStatus.refresh()')
    expect(page.locator('.global-now-playing')).to_have_class(re.compile('is-onboarding-cue'))
    expect(byline).to_have_text('Your first set is ready')
    page.locator('.topbar nav').get_by_role('link', name='Home', exact=True).click()
    expect(byline).to_have_text('Enjoy your first set while we prepare your account')
    page.reload()
    expect(page.locator('.onboarding-radio .radio-home')).to_be_visible()
    expect(byline).to_have_text('Enjoy your first set while we prepare your account')
    expect(byline).to_be_visible()
    page.evaluate('''async () => {
        await document.fonts.ready;
        await Promise.allSettled(document.getAnimations().filter(a =>
            a.effect.getComputedTiming().iterations !== Infinity).map(a => a.finished));
        await new Promise(requestAnimationFrame);
    }''')
    expect(byline).to_be_visible()
    assert page.locator('.festival-intro-copy').evaluate('''el => el.getBoundingClientRect().bottom
        < document.querySelector('.festival-intro .landmark-hit').getBoundingClientRect().top''')
    page.screenshot(path=f'/tmp/tuneconsole-564-byline-radio-{width}.png', full_page=True)
    assert not errors


@pytest.mark.parametrize('width', [1440])
def test_extension_setup_and_live_import_details(live_app, page, width):
    connected = {"connected": False, "now_playing": None, "radio": False}
    page.route("**/bridge/status", lambda route: route.fulfill(json=connected))
    status = {"songs": 0, "covers": [], "playlists": 0, "albums": 0, "outcome": None,
              "setup": {"complete": False, "imported": False, "running": False,
                        "analyzed": 0, "total": 0, "mixes_ready": False}}
    log = {"events": [], "outcome": None}
    page.route("**/home/onboard/status", lambda route: route.fulfill(json=status))
    page.route("**/sync/recent", lambda route: route.fulfill(json=log))
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app)
    expect(page.locator('#arrival-title')).to_have_text('Welcome to TuneConsole')
    expect(page.locator('.festival-intro .festival-map')).to_be_visible()
    expect(page.locator('.festival-destination[aria-label="Go to my first set"]')).to_be_visible()
    assert page.locator('#arrival-title').evaluate('el => getComputedStyle(el).textAlign') == 'left'
    first = page.locator('.landmark-art.is-soundcheck')
    expect(first).to_have_class(re.compile('is-untouched'))
    assert first.locator('.landmark-halo').evaluate('el => getComputedStyle(el).animationName') == 'none'
    assert first.locator('.landmark-halo').evaluate('el => getComputedStyle(el).opacity') == '0'
    expect(page.locator('.festival-routes .growth-route:visible')).to_have_count(0)
    assert page.locator('body').evaluate('body => body.scrollWidth <= innerWidth')
    install = page.get_by_role("link", name="Get the extension")
    expect(install).to_be_visible()
    expect(page.locator(".arrival-progress")).to_be_hidden()
    expect(page.get_by_role("link", name="YouTube Music", exact=True)).to_be_visible()
    page.locator("main").evaluate("el => Promise.all(el.getAnimations({subtree: true}).filter(a => a.effect.getComputedTiming().iterations !== Infinity).map(a => a.finished))")
    page.screenshot(path=f'/tmp/tuneconsole-551-festival-entry-{width}.png', full_page=True)
    page.locator(".setup-banner").get_by_role("button", name="Details").click()
    details = page.get_by_role("dialog")
    expect(details).to_be_visible()
    expect(details).to_contain_text("Connect the extension to get started")

    connected["connected"] = True
    status["setup"].update(running=True, activity="playlists", current=2, target=5)
    log["events"].append({"type": "info", "text": "Reading playlist 2 of 5: Confessionals"})
    page.evaluate("window.dispatchEvent(new CustomEvent('bridge-status', {detail: {connected: true}}))")
    expect(page.locator('#arrival-title')).to_have_text('Welcome to TuneConsole')
    expect(install).to_be_hidden()
    expect(page.locator(".setup-banner-step")).to_have_text("Reading playlist 2 of 5", timeout=6500)
    expect(page.locator(".arrival-progress")).to_be_visible()
    expect(page.locator(".arrival-progress")).to_contain_text("Reading playlist 2 of 5")
    expect(details.get_by_text("Reading playlist 2 of 5: Confessionals")).to_be_visible(timeout=6500)
    for preference in ("reduce", "no-preference"):
        page.emulate_media(reduced_motion=preference)
        animation = page.locator(".setup-banner").evaluate("""el => {
            const style = getComputedStyle(el, '::after');
            return [style.animationName, style.animationDuration,
                    style.animationIterationCount, style.animationPlayState];
        }""")
        assert animation == ["setup-working", "4s", "infinite", "running"], preference
    assert first.locator('.landmark-halo').evaluate('el => getComputedStyle(el).animationName') == 'none'
    status["setup"]["running"] = False
    status["outcome"] = "partial"
    log["events"].append({"type": "err", "text": "Library partially updated."})
    expect(page.locator(".setup-banner-step")).to_have_text("Keep YouTube Music open", timeout=6500)
    expect(details.get_by_text("Library partially updated.", exact=True)).to_be_visible()

    connected["connected"] = False
    page.evaluate("window.dispatchEvent(new CustomEvent('bridge-status', {detail: {connected: false}}))")
    status["setup"]["imported"] = True
    expect(page.locator(".arrival-progress")).to_contain_text("Open YouTube Music to continue setup", timeout=6500)
    expect(page.locator(".arrival-progress")).to_be_visible()
    assert page.locator(".arrival-wave > span").first.evaluate("el => getComputedStyle(el).animationName === 'none'")
    expect(first).to_have_class(re.compile('is-untouched'))
    assert first.locator('.landmark-halo').evaluate('el => getComputedStyle(el).opacity') == '0'
    assert errors == []


@pytest.mark.parametrize("width", [1440])
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
