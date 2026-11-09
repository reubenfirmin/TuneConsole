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
    expect(byline).to_have_text('Enjoy your first set while we find more music for you')
    page.reload()
    expect(byline).to_be_visible()
    expect(byline).to_have_text('Enjoy your first set while we find more music for you')
    assert store.settings.get_setting('last_sync_at') is None
    assert errors == []
