"""Document shortcuts reach the connected player after album interactions."""
import json

import pytest
from playwright.sync_api import expect
from websockets.sync.client import connect

from tests.browser.test_artist_album_browser import live_artist_app  # noqa: F401
from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN

pytestmark = pytest.mark.browser


@pytest.mark.parametrize('width', [1280, 390])
def test_album_space_and_media_actions_reach_the_connected_player(live_artist_app, page, width):
    base = live_artist_app['base']
    page.set_viewport_size({'width': width, 'height': 1000})
    page.add_init_script('''
        window.mediaActions = {};
        const setActionHandler = navigator.mediaSession.setActionHandler.bind(navigator.mediaSession);
        navigator.mediaSession.setActionHandler = (name, callback) => {
            window.mediaActions[name] = callback;
            setActionHandler(name, callback);
        };
    ''')
    page.route('**/play', lambda route: route.fulfill(json={'ok': True}))
    with connect(base.replace('http://', 'ws://') + '/bridge/ws',
                 origin=EXTENSION_ORIGIN, open_timeout=5) as extension:
        def heartbeat(paused):
            extension.send(json.dumps({'type': 'now-heartbeat', 'videoId': 'v1',
                'title': 'SongA', 'artist': 'Artist X', 'paused': paused}))

        def commanded(action, paused):
            frame = json.loads(extension.recv(timeout=5))
            assert frame['type'] == 'playpause'
            assert frame.get('action', 'toggle') == action
            heartbeat(paused)
            page.evaluate('window.playbackStatus.refresh()')
            expect(player.get_by_role('button', name='Play' if paused else 'Pause', exact=True)).to_be_visible()
            expect(page).to_have_url(base + '/album?browse=MPREb_x')

        heartbeat(False)
        page.goto(base + '/album?browse=MPREb_x')
        player = page.locator('.global-now-playing')
        expect(player).to_be_visible()
        # A playback link stays focused after its click is handed to the extension.
        # Space has no native activation on an anchor and must still pause music.
        play = page.locator('.track-table a.pl-play').first
        play.click()
        expect(play).to_be_focused()
        with page.expect_request('**/now-playing/toggle', timeout=3000):
            page.keyboard.press('Space')
        commanded('toggle', True)
        with page.expect_request('**/now-playing/toggle', timeout=3000):
            page.keyboard.press('MediaPlayPause')
        commanded('toggle', False)
        page.evaluate('window.mediaActions.pause()')
        commanded('pause', True)
        page.evaluate('window.mediaActions.play()')
        commanded('play', False)
        # A different page and browser history restore the same document controls.
        page.goto(base + '/artists')
        expect(page.locator('.global-now-playing')).to_be_visible()
        page.go_back()
        expect(player).to_be_visible()
        page.locator('.track-table a.pl-play').first.focus()
        page.keyboard.press('Space')
        commanded('toggle', True)
