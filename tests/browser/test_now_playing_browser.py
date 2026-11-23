"""Space controls playback while respecting search and focused buttons."""
import json

import pytest
from playwright.sync_api import expect
from websockets.sync.client import connect

from tests.home_fixture_support import returning_listener
from tests.onboarding_support import publish_personal_mix
from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN


def test_now_playing_retains_the_song_during_a_status_read_failure(live_app, page):
    song = {'video_id': 'playing', 'title': 'Still playing', 'artist': 'Artist',
            'paused': False, 'position': 40, 'duration': 180}
    status = {'connected': True, 'now_playing': song}
    page.route('**/bridge/status', lambda route: route.fulfill(
        json=status) if status is not None else route.fulfill(status=503))
    commands = []
    page.on('request', lambda request: commands.append(request.url)
            if request.method == 'POST' and '/now-playing/' in request.url else None)

    page.goto(live_app + '/artists')
    card = page.locator('.global-now-playing')
    expect(card.locator('.hs-now-title')).to_have_text('Still playing')
    expect(card.get_by_role('progressbar', name='Track progress')).to_be_visible()
    expect(card.get_by_role('button', name='Pause', exact=True)).to_be_enabled()

    status = None
    page.evaluate('window.playbackStatus.refresh()')
    expect(card.locator('.hs-now-title')).to_have_text('Still playing')
    expect(card.get_by_role('progressbar', name='Track progress')).to_be_hidden()
    expect(card.get_by_role('button', name='Play or pause')).to_be_disabled()
    expect(card.get_by_role('button', name='Next / skip')).to_be_disabled()
    assert page.evaluate("window.playbackControls.command('toggle')") is False
    assert not commands

    status = {'connected': True, 'now_playing': {**song, 'video_id': 'next', 'title': 'Next song'}}
    page.evaluate('window.playbackStatus.refresh()')
    expect(card.locator('.hs-now-title')).to_have_text('Next song')
    expect(card.get_by_role('button', name='Pause', exact=True)).to_be_enabled()
    status = {'connected': False}
    page.evaluate('window.playbackStatus.refresh()')
    expect(card).to_be_hidden()


@pytest.mark.parametrize('selected,other', [
    (('On air', 'radio', 'ArrowLeft'), ('Mix tapes', 'directions', 'ArrowRight')),
    (('Mix tapes', 'directions', 'ArrowRight'), ('On air', 'radio', 'ArrowLeft')),
], ids=['on-air', 'mix-tapes'])
def test_space_pauses_and_resumes_radio_while_home_tab_keeps_focus(live_app, page, store, selected, other):
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')
    tracks = publish_personal_mix(store, now=1)
    returning_listener(store, 'radio', 'mixes')
    store.settings.set_setting('radio_seed_offer', json.dumps({'tracks': tracks, 'menu_id': 'keyboard'}))

    requests = []
    page.on('request', lambda request: requests.append(request.url.removeprefix(live_app))
            if request.method == 'POST' and request.url.endswith(('/home/format', '/now-playing/toggle'))
            else None)

    with connect(live_app.replace('http://', 'ws://') + '/bridge/ws',
                 origin=EXTENSION_ORIGIN, open_timeout=5) as extension:
        def heartbeat(paused):
            extension.send(json.dumps({'type': 'now-heartbeat', 'videoId': 'radio-song',
                'title': 'Playing on Radio', 'artist': 'Artist', 'playlist': 'PLRADIO', 'paused': paused}))

        heartbeat(False)
        tab_name, tab_area, next_key = selected
        other_name, other_area, previous_key = other
        page.goto(f'{live_app}/#{other_area}')
        player = page.locator('.global-now-playing')
        expect(player.get_by_role('button', name='Pause', exact=True)).to_be_visible()
        tab = page.get_by_role('tab', name=tab_name, exact=True)
        other_tab = page.get_by_role('tab', name=other_name, exact=True)
        expect(other_tab).to_have_attribute('aria-selected', 'true')
        with page.expect_response('**/home/format'):
            tab.click()
        expect(tab).to_be_focused()
        expect(tab).to_have_attribute('aria-selected', 'true')

        for paused in (True, False):
            with page.expect_response('**/now-playing/toggle', timeout=3000):
                if paused:
                    page.keyboard.down('Space')
                    page.keyboard.down('Space')  # A held key repeats before release.
                    page.keyboard.up('Space')
                else:
                    page.keyboard.press('Space')
            frame = json.loads(extension.recv(timeout=5))
            assert frame['type'] == 'playpause' and frame.get('action', 'toggle') == 'toggle'
            heartbeat(paused)
            page.evaluate('window.playbackStatus.refresh()')
            expect(player.get_by_role('button', name='Play' if paused else 'Pause', exact=True)).to_be_visible()
            expect(tab).to_be_focused()
            expect(tab).to_have_attribute('aria-selected', 'true')
            expect(page).to_have_url(f'{live_app}/#{tab_area}')

        assert requests == ['/home/format', '/now-playing/toggle', '/now-playing/toggle']
        tab.press(next_key)
        expect(other_tab).to_be_focused()
        expect(tab).to_have_attribute('aria-selected', 'true')
        with page.expect_response('**/home/format'):
            page.keyboard.press('Space')
        expect(other_tab).to_have_attribute('aria-selected', 'true')
        other_tab.press(previous_key)
        expect(tab).to_be_focused()
        with page.expect_response('**/home/format'):
            page.keyboard.press('Space')
        expect(tab).to_have_attribute('aria-selected', 'true')
        assert requests[3:] == ['/home/format', '/home/format']


def test_space_controls_playback_without_intercepting_search_or_focused_buttons(live_app, page, store):
    status = {'connected': True, 'now_playing': {
        'video_id': 'keyboard', 'title': 'Song', 'artist': 'Artist', 'paused': False}}
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    commands = []

    def toggle(route):
        commands.append(route.request.url)
        status['now_playing']['paused'] = not status['now_playing']['paused']
        route.fulfill(json={'ok': True})

    page.route('**/now-playing/toggle*', toggle)
    page.goto(live_app + '/artists')
    player = page.locator('.global-now-playing')
    expect(player).to_be_visible()
    page.locator('body').click(position={'x': 600, 'y': 600})
    page.keyboard.press('Space')
    expect(player.get_by_role('button', name='Play', exact=True)).to_be_visible()
    assert len(commands) == 1
    page.get_by_role('button', name='Search music…', exact=True).click()
    search = page.get_by_role('searchbox', name='Search music', exact=True)
    search.fill('two')
    search.press('Space')
    expect(search).to_have_value('two ')
    assert len(commands) == 1
    page.get_by_role('button', name='Close music search', exact=True).click()
    player.get_by_role('button', name='Play', exact=True).focus()
    page.keyboard.press('Space')
    expect(player.get_by_role('button', name='Pause', exact=True)).to_be_visible()
    assert len(commands) == 2
    account = store.accounts.get_accounts()[0].id
    pid = store.playlists.upsert_playlist(account, 'PLKEYBOARD', 'Keyboard mix', 2, 'h', 1)
    tracks = [store.tracks.upsert_track(video, video, 'Artist', '', 180, 1)
              for video in ('keyboard', 'other')]
    store.playlists.set_playlist_tracks(pid, tracks)
    ratings = []

    def rate(route):
        ratings.append(route.request.post_data)
        if route.request.url.endswith('/now-playing/rate'):
            status['now_playing']['likeStatus'] = route.request.post_data_json['likeStatus']
            route.fulfill(json={'ok': True})
        else:
            route.fulfill(body='')

    page.route('**/track/like', rate)
    page.route('**/now-playing/rate', rate)
    page.goto(f'{live_app}/playlist/{pid}')
    for video in ('keyboard', 'other'):
        heart = page.locator(f'tr[data-vid="{video}"] .like-btn')
        for pressed in ('true', 'false'):
            with page.expect_request(lambda request: request.url.endswith(('/track/like', '/now-playing/rate'))):
                heart.click()
            expect(heart).to_have_attribute('aria-pressed', pressed)
            before = len(ratings)
            toggles = len(commands)
            with page.expect_request('**/now-playing/toggle*'):
                page.keyboard.press('Space')
            expect(player.get_by_role('button', name='Play' if status['now_playing']['paused'] else 'Pause', exact=True)).to_be_visible()
            assert len(commands) == toggles + 1
            assert len(ratings) == before
            expect(heart).to_have_attribute('aria-pressed', pressed)
        # Deliberate keyboard focus retains native Space/Enter rating activation.
        heart.focus()
        toggles = len(commands)
        for key, pressed in (('Space', 'true'), ('Enter', 'false')):
            with page.expect_request(lambda request: request.url.endswith(('/track/like', '/now-playing/rate'))):
                page.keyboard.press(key)
            expect(heart).to_have_attribute('aria-pressed', pressed)
            expect(heart).to_be_focused()
            assert len(commands) == toggles
