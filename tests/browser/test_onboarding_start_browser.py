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
    from tests.onboarding_support import finish_partial_import, publish_live_mix_inputs

    finish_partial_import(store, now=1)
    page.route('**/bridge/status', lambda route: route.fulfill(json={
        'connected': True, 'now_playing': {'video_id': 'still-playing', 'title': 'Still playing',
                                          'artist': 'Artist', 'paused': False}}))
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app)
    page.get_by_role('button', name='Explore my festival', exact=True).click()
    expect(page.locator('#onboarding-feed')).to_be_visible()
    expect(page.locator('.global-now-playing')).to_contain_text('Still playing')
    page.evaluate('window.arrivalDocument = document; window.arrivalPlayer = document.querySelector(".global-now-playing")')
    from tests.test_growth_journey import early_journey
    reserve = early_journey(store)
    publish_live_mix_inputs(store, now=1, fresh_reserve=reserve)
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




def test_returning_home_recovers_a_late_mix_without_restarting_setup(live_app, store, page, tmp_path):
    from tests.conftest import seed_catalog_track
    from tests.onboarding_support import publish_live_mix_inputs
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
    ready_cards = board.locator('.gen-card:has(.gen-row)')
    expect(board).to_have_attribute('hx-trigger', 'every 30s')
    expect(page.locator('#onboarding-feed')).to_have_count(0)
    expect(page.locator('.setup-banner')).to_be_hidden()
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
    publish_live_mix_inputs(store, now=1, count=1)
    with page.expect_response('**/home/cards?prepare_attempt=10'):
        page.clock.fast_forward(30_000)
    expect(ready_cards).to_have_count(1)
    expect(board.locator('.gen-row')).to_have_count(4)
    expect(page.get_by_role('tab', name='Mix tapes', exact=True)).to_be_visible()
    expect(page.locator('.mix-preparation-note')).to_be_hidden()
    # One usable playlist dismisses the preparation note while the incomplete
    # board keeps polling for its remaining lanes.
    expect(board).to_have_attribute('hx-get', '/home/cards?prepare_attempt=10')
    expect(board).to_have_attribute('hx-trigger', 'every 30s')
    page.clock.run_for(2000)

    # Publish the remaining ranks so the next ordinary request can assemble all four cards.
    publish_live_mix_inputs(store, now=2)
    with page.expect_response('**/home/cards?prepare_attempt=10'):
        page.clock.fast_forward(30_000)
    expect(ready_cards).to_have_count(4)
    expect(board.locator('.gen-row')).to_have_count(16)
    expect(page.locator('#home-mode-cards[hx-get]')).to_have_count(0)
    expect(page.locator('#home-mode-cards[hx-trigger]')).to_have_count(0)
    expect(page.locator('#onboarding-feed')).to_have_count(0)
    expect(page.locator('.global-now-playing')).to_contain_text('Still playing')
    assert page.evaluate('startupDocument === document && startupPlayer === document.querySelector(".global-now-playing")')
    with page.expect_response('**/home/growth?navigation=true') as growth:
        page.clock.fast_forward(15_000)
    assert growth.value.ok
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














@pytest.mark.parametrize(('width', 'height'), [(1280, 1000), (960, 540)])
def test_first_album_is_playable_before_sync_and_cues_only_its_player(live_app, store, page, width, height, tmp_path):
    from urllib.parse import parse_qs
    from tests.browser.test_album_listening_browser import _COVER

    store.settings.set_setting('onboard_listening_format', 'albums')
    store.collection.add_saved_album({'browse': 'MPRE_starter', 'title': 'The first record',
                                      'artist': 'An artist', 'year': '2001'})
    status = {'connected': True, 'now_playing': {'video_id': 'other', 'title': 'Other song',
        'artist': 'Another artist', 'paused': False, 'playlist_ytm_id': 'OTHER'}}
    page.set_viewport_size({'width': width, 'height': height})
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    page.route('**/onboarding-album-cover.svg', lambda route: route.fulfill(body=_COVER, content_type='image/svg+xml'))
    page.route('**/album/play', lambda route: route.fulfill(status=204, headers={'HX-Trigger': json.dumps({
        'album-play-started': {'browse': 'MPRE_starter', 'playlist': 'ALBUM_QUEUE',
            'title': 'The first record', 'artist': 'An artist', 'thumbnail': '/onboarding-album-cover.svg'}})}))
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(live_app)
    welcome = page.get_by_role('dialog', name='A better way to listen to music.')
    expect(welcome).to_be_visible()
    page.screenshot(path='/tmp/tuneconsole-1259-welcome.png')
    page.get_by_role('button', name='Explore my festival', exact=True).click()
    expect(welcome).to_be_hidden()
    card = page.locator('.starter-album')
    expect(card).to_be_visible()
    byline = page.locator('.festival-intro-sub')
    expect(byline).to_have_text('Your first set is ready')
    expect(card).to_contain_text('The first record')
    expect(card.locator('.starter-context')).to_have_text(
        'Here’s an album to listen to while we find more music for you.')
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
    expect(page.locator('.onboarding-player-tour')).to_have_count(0)
    # A song with album metadata in an unrelated playlist cannot satisfy the handoff.
    status['now_playing'].update(title='An album song', album_browse_id='MPRE_starter',
        source={'kind': 'playlist', 'url': '/playlist/1'})
    page.evaluate('window.playbackStatus.refresh()')
    expect(player).not_to_have_class(re.compile('is-onboarding-cue'))
    expect(byline).to_have_text('Your first set is ready')
    status['now_playing'].update(video_id='first', artist='An artist', playlist_ytm_id='ALBUM_QUEUE', paused=True,
        position=12, duration=200,
        source={'kind': 'album', 'url': '/album?browse=MPRE_starter', 'title': 'The first record'})
    page.evaluate('window.playbackStatus.refresh()')
    expect(player).not_to_have_class(re.compile('is-onboarding-cue'))
    expect(byline).to_have_text('Your first set is ready')
    status['now_playing']['paused'] = False
    page.evaluate('window.playbackStatus.refresh()')
    expect(player).to_have_class(re.compile('is-onboarding-cue'))
    dialog = page.get_by_role('dialog', name='The first record', exact=True)
    expect(dialog).to_be_visible()
    tour = page.get_by_role('region', name='Your music is playing', exact=True)
    expect(tour).to_be_visible()
    assert tour.evaluate('el => !!el.closest("dialog.album-listening[open]")')
    expect(dialog.locator('.album-listening-cover')).to_have_js_property('naturalWidth', 900)
    page.screenshot(path=f'/tmp/tuneconsole-1259-album-tour-{width}.png')
    # Exercise the actual first-play screen before closing its artwork.
    tour.get_by_role('button', name='Next', exact=True).click()
    expect(page.get_by_role('region', name='Keep listening', exact=True)).to_be_visible()
    page.keyboard.press('Escape')
    expect(dialog).to_be_hidden()
    assert page.locator('.onboarding-player-tour').evaluate('el => el.parentElement === document.body')
    page.get_by_role('button', name='Show album artwork', exact=True).click()
    expect(dialog.get_by_role('region', name='Keep listening', exact=True)).to_be_visible()
    page.reload()
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role('region', name='Keep listening', exact=True)).to_be_visible()
    page.get_by_role('region', name='Keep listening', exact=True).get_by_role('button', name='Next').click()
    ratings_tour = page.get_by_role('region', name='Make it yours', exact=True)
    expect(ratings_tour).to_contain_text('Dislike skips a song')
    page.screenshot(path=f'/tmp/tuneconsole-1259-album-ratings-tour-{width}.png')
    with page.expect_response('**/onboard/player-tour/dismiss'):
        ratings_tour.get_by_role('button', name='Dismiss tour' if width == 960 else 'Got it', exact=True).click()
    expect(page.locator('.onboarding-player-tour')).to_have_count(0)
    page.keyboard.press('Escape')
    page.locator('.topbar nav').get_by_role('link', name='Home', exact=True).click()
    page.reload()
    expect(page.get_by_role('dialog', name='A better way to listen to music.')).to_have_count(0)
    expect(page.locator('.onboarding-player-tour')).to_have_count(0)
    expect(byline).to_be_visible()
    expect(byline).to_have_text('Enjoy your first set while we find more music for you')
    assert store.settings.get_setting('last_sync_at') is None
    assert errors == []


def test_import_header_uses_completed_batch_progress_when_readiness_is_unavailable(
        live_initial_sync, store, page):
    live_app, job = live_initial_sync
    store.settings.set_setting('onboard_welcome_dismissed', '1')
    job.events.append({'type': 'info', 'text': 'Reading playlist 2 of 5',
                       'stage': 'playlists', 'i': 2, 'total': 5, 'completed': 1})
    page.route('**/home/onboard/status', lambda route: route.fulfill(status=503))
    page.set_viewport_size({'width': 1440, 'height': 1000})
    with page.expect_response('**/home/onboard/status') as readiness:
        page.goto(f'{live_app}/playlists')
    assert readiness.value.status == 503
    banner = page.locator('.setup-banner')
    expect(banner).to_be_visible()
    expect(banner).to_contain_text('Reading playlist 2 of 5')
    progress = banner.get_by_role('progressbar', name='Current import batch')
    expect(progress).to_have_attribute('value', '1')
    expect(progress).to_have_attribute('max', '5')
    assert progress.evaluate('el => getComputedStyle(el, "::after").animationName') == 'none'
    page.evaluate('window.initialHeader = document.querySelector(".setup-status")')
    with page.expect_response(lambda response: response.url == f'{live_app}/' and response.ok):
        page.locator('.topbar nav').get_by_role('link', name='Home', exact=True).click()
    # The navigation fetch returns before Home's assets load and its feed mounts.
    expect(page.locator('#onboarding-feed')).to_be_visible(timeout=20_000)
    assert page.evaluate('initialHeader === document.querySelector(".setup-status")')
    job.events.append({'type': 'step', 'text': 'Playlist imported',
                       'stage': 'playlists', 'i': 3, 'total': 5, 'completed': 3})
    expect(progress).to_have_attribute('value', '3', timeout=7000)
    page.screenshot(path='/tmp/tuneconsole-1259-import-header.png')


def test_generated_first_sounds_tour_survives_the_play_redirect(live_app, store, page):
    from tests.conftest import seed_catalog_track

    for i in range(3):
        seed_catalog_track(store, f'first {i}|artist', f'First song {i}', 'Artist', video_id=f'first-{i}')
    status = {'connected': True, 'now_playing': {'video_id': 'unrelated', 'title': 'Other music',
              'artist': 'Artist', 'paused': False, 'playlist_ytm_id': 'OTHER'}}
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    page.set_viewport_size({'width': 1440, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(live_app)
    page.get_by_role('button', name='Explore my festival', exact=True).click()
    # Keep the actual HTMX redirect and destination document lifecycle. Provider
    # mutation and continuation safety are verified separately with real Stores.
    def saved(route):
        route.fulfill(status=200, headers={'HX-Redirect': '/playlists',
            'HX-Trigger': json.dumps({'generated-playlist-saved': {'playlist': 'FIRST_SOUNDS'}})})
    page.route('**/home/generate', saved)
    with page.expect_popup() as popup:
        page.locator('[data-onboarding-play]').click()
    popup.value.close()
    expect(page).to_have_url(f'{live_app}/playlists')
    expect(page.locator('.onboarding-player-tour')).to_have_count(0)
    assert page.evaluate("JSON.parse(sessionStorage.getItem('tc-onboarding-playback')).playlist") == 'FIRST_SOUNDS'
    status['now_playing'].update(video_id='first-0', title='First song 0', playlist_ytm_id='FIRST_SOUNDS', paused=True)
    page.evaluate('window.playbackStatus.refresh()')
    expect(page.locator('.onboarding-player-tour')).to_have_count(0)
    status['now_playing']['paused'] = False
    page.evaluate('window.playbackStatus.refresh()')
    tour = page.get_by_role('region', name='Your music is playing', exact=True)
    expect(tour).to_be_visible()
    page.clock.install()
    page.clock.fast_forward(9000)
    expect(tour).to_be_visible()
    page.screenshot(path='/tmp/tuneconsole-1259-generated-tour.png')
    assert errors == []
