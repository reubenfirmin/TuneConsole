"""Album playback makes its cover primary without creating a second player."""
import socket
import threading
from types import SimpleNamespace

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.conftest import FakeClient
from tests.live_server import LiveServer
from yt_playlist.core.bridge import Bridge
from yt_playlist.library.album_fetch import UPLOAD_RELEASE_PREFIX
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.browser

_COVER = '''<svg xmlns="http://www.w3.org/2000/svg" width="900" height="900" viewBox="0 0 900 900">
<defs><linearGradient id="sky" x2="0" y2="1"><stop stop-color="#152940"/>
<stop offset="1" stop-color="#d68d72"/></linearGradient></defs>
<path fill="url(#sky)" d="M0 0h900v900H0z"/><circle cx="450" cy="440" r="200" fill="#edc095"/>
<path fill="#263846" d="m0 650 250-230 220 260 200-160 230 210v170H0z"/>
<path fill="#172b3c" d="m0 790 330-140 300 140 270-80v190H0z"/>
<text x="65" y="110" fill="#f7e4cd" font-family="sans-serif" font-size="54" letter-spacing="12">LATE NIGHT</text>
<text x="65" y="835" fill="#f7e4cd" font-family="sans-serif" font-size="22" letter-spacing="6">THE EVENING SESSIONS</text>
</svg>'''


@pytest.fixture
def album_app(store, without_metadata_providers, monkeypatch):
    identity = store.accounts.upsert_account('Main', None, True)
    album = {'audioPlaylistId': 'ALBUM_QUEUE', 'title': 'Late Night', 'type': 'Album',
        'artists': [{'name': 'The Evening Sessions'}], 'year': '2001',
        'thumbnails': [{'url': '/album-cover.svg', 'width': 900, 'height': 900}],
        'tracks': [{'videoId': 'one', 'title': 'Opening', 'duration': '3:20',
                    'artists': [{'name': 'The Evening Sessions'}]},
                   {'videoId': 'two', 'title': 'After Hours', 'duration': '3:00'}]}
    upload = {**album, 'audioPlaylistId': None, 'title': 'Private release'}
    client = FakeClient(albums={'MPRE_album': album}, uploads={UPLOAD_RELEASE_PREFIX + '_album': upload})
    sent = []
    bridge = Bridge()
    bridge.connect(lambda message: None, None)
    monkeypatch.setattr(bridge, '_queue_control', sent.append)
    app = create_app(store, lambda: {identity: client}, bridge=bridge, background_work=False)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
        with LiveServer(server, sockets=[sock]):
            yield SimpleNamespace(base=f'http://127.0.0.1:{port}', sent=sent, bridge=bridge, client=client)


def _player(page):
    status = {'connected': True, 'now_playing': {
        'video_id': 'one', 'title': 'Opening', 'artist': 'The Evening Sessions',
        'paused': False, 'playlist_ytm_id': 'OTHER_QUEUE', 'album_browse_id': 'MPRE_album',
        'likeStatus': 'INDIFFERENT', 'thumbnail': '/album-cover.svg', 'position': 30, 'duration': 200,
        'source': {'kind': 'album', 'url': '/album?browse=MPRE_album', 'title': 'Late Night'}}}
    page.route('**/bridge/status', lambda route: route.fulfill(json=status))
    page.route('**/album-cover.svg', lambda route: route.fulfill(body=_COVER, content_type='image/svg+xml'))
    return status


def test_album_cover_primary_and_shared_controls_return_to_tracks(album_app, page):
    page.set_viewport_size({'width': 1440, 'height': 900})
    status = _player(page)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(album_app.base + '/album?browse=MPRE_album')
    dialog = page.get_by_role('dialog', name='Late Night', exact=True)
    expect(page.locator('.hs-now-title')).to_have_text('Opening')
    # Album metadata alone is not an album playback request.
    expect(dialog).to_be_hidden()
    with page.expect_response('**/album/play') as response:
        page.get_by_role('button', name='Play Late Night', exact=True).click()
    assert response.value.status == 204
    assert album_app.sent == [{'type': 'navigate',
        'url': 'https://music.youtube.com/watch?v=one&list=ALBUM_QUEUE'}]
    expect(dialog).to_be_hidden()
    status['now_playing'].update(playlist_ytm_id='ALBUM_QUEUE', paused=True)
    expect(page.get_by_role('button', name='Play Late Night', exact=True)).to_be_visible()
    with page.expect_response('**/bridge/status'):
        pass
    expect(dialog).to_be_hidden()
    status['now_playing']['paused'] = False
    page.evaluate('window.playbackStatus.refresh()')
    expect(dialog).to_be_visible(timeout=6000)
    cover = dialog.locator('.album-listening-cover')
    expect(cover).to_have_js_property('naturalWidth', 900)
    expect(cover).to_have_attribute('src', '/album-cover.svg')
    for width, height in ((1920, 1080), (960, 540), (1440, 900)):
        page.set_viewport_size({'width': width, 'height': height})
        assert dialog.bounding_box() == {'x': 0, 'y': 0, 'width': width, 'height': height}
        assert cover.bounding_box()['height'] > height * .68
        expect(dialog.get_by_role('link', name='Back to tracks', exact=True)).to_be_in_viewport()
        expect(dialog.get_by_role('button', name='Pause', exact=True)).to_be_in_viewport()
        page.screenshot(path=f'/tmp/tuneconsole-1262-album-{width}.png')

    def toggle(route):
        status['now_playing']['paused'] = not status['now_playing']['paused']
        route.fulfill(json={'ok': True})

    page.route('**/now-playing/toggle*', toggle)
    dialog.get_by_role('button', name='Pause', exact=True).click()
    expect(dialog.get_by_role('button', name='Play', exact=True)).to_be_enabled()
    dialog.get_by_role('button', name='Play', exact=True).click()
    expect(dialog.get_by_role('button', name='Pause', exact=True)).to_be_enabled()
    ratings = []

    def rate(route):
        ratings.append(route.request.post_data_json)
        status['now_playing']['likeStatus'] = ratings[-1]['likeStatus']
        route.fulfill(json={'ok': True})

    page.route('**/now-playing/rate', rate)
    with page.expect_response('**/now-playing/rate'):
        dialog.get_by_role('button', name='Like', exact=True).click()
    expect(dialog.get_by_role('button', name='Like', exact=True)).to_have_attribute('aria-pressed', 'true')
    expect(page.locator('.hs-now .hs-heart')).to_have_attribute('aria-pressed', 'true')
    with page.expect_response('**/now-playing/rate'):
        dialog.get_by_role('button', name='Like', exact=True).click()
    assert [rating['likeStatus'] for rating in ratings] == ['LIKE', 'INDIFFERENT']

    status['now_playing'].update(video_id='two', title='After Hours', thumbnail='/different-track-cover.svg')
    page.evaluate('window.playbackStatus.refresh()')
    expect(dialog.locator('.album-listening-track a')).to_have_text('After Hours', timeout=6000)
    expect(cover).to_have_attribute('src', '/album-cover.svg')
    # Reload keeps an explicitly opened album view, without sending playback again.
    page.reload()
    expect(dialog).to_be_visible()
    assert len(album_app.sent) == 1
    page.keyboard.press('Escape')
    expect(dialog).to_be_hidden()
    page.evaluate('window.playbackStatus.refresh()')
    expect(dialog).to_be_hidden()
    page.reload()
    expect(dialog).to_be_hidden()
    page.get_by_role('button', name='Show album artwork').click()
    expect(dialog).to_be_visible()
    dialog.get_by_role('link', name='Back to tracks', exact=True).click()
    expect(page).to_have_url(album_app.base + '/album?browse=MPRE_album#album-tracks')
    expect(dialog).to_be_hidden()
    expect(page.locator('#album-tracks .album-track-table')).to_be_visible()
    expect(page.locator('.hs-now-title')).to_have_text('After Hours')
    assert len(album_app.sent) == 1

    page.get_by_role('button', name='Show album artwork').click()
    expect(dialog).to_be_visible()
    # A missing status sample keeps the cover and its exit available.
    song = status.pop('now_playing')
    page.evaluate('window.playbackStatus.refresh()')
    assert page.evaluate('window.playbackStatus.snapshot?.now_playing ?? null') is None
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role('link', name='Back to tracks', exact=True)).to_be_visible()
    status['now_playing'] = song
    page.evaluate('window.playbackStatus.refresh()')
    status['now_playing'].update(playlist_ytm_id='RADIO',
        source={'kind': 'radio', 'url': '/#radio', 'title': 'On Air'})
    page.evaluate('window.playbackStatus.refresh()')
    expect(dialog).to_be_hidden(timeout=6000)
    expect(page.get_by_role('button', name='Show album artwork')).to_be_hidden()
    assert errors == []


def test_failed_album_start_and_missing_artwork_keep_playback_usable(album_app, page):
    status = _player(page)
    # Fail the first image load; a previously decoded successful image can be
    # reused by Chromium even when a later route is changed to return 404.
    failures = []
    page.route('**/album-cover.svg', lambda route: route.fulfill(status=404))
    page.on('response', lambda response: failures.append(response.status)
            if response.url.endswith('/album-cover.svg') else None)
    page.goto(album_app.base + '/album?browse=MPRE_album')
    dialog = page.get_by_role('dialog', name='Late Night', exact=True)
    album_app.bridge.disconnect()
    with page.expect_response('**/album/play') as response:
        page.get_by_role('button', name='Play Late Night', exact=True).click()
    assert response.value.status == 503
    status['now_playing']['playlist_ytm_id'] = 'ALBUM_QUEUE'
    with page.expect_response('**/bridge/status'):
        pass
    expect(dialog).to_be_hidden()
    expect(page.get_by_role('button', name='Show album artwork')).to_be_hidden()
    # Start a new album request, then let its exact queue arrive without artwork.
    status['now_playing']['playlist_ytm_id'] = 'OTHER_QUEUE'
    expect(page.get_by_role('button', name='Play Late Night', exact=True)).to_be_visible(timeout=6000)
    album_app.bridge.connect(lambda message: None, None)
    with page.expect_response('**/album/play') as response:
        page.get_by_role('button', name='Play Late Night', exact=True).click()
    assert response.value.status == 204
    status['now_playing']['playlist_ytm_id'] = 'ALBUM_QUEUE'
    expect(dialog).to_be_visible(timeout=6000)
    expect(dialog.get_by_role('img', name='Album artwork unavailable')).to_be_visible()
    assert failures and all(code == 404 for code in failures)
    expect(dialog.get_by_role('button', name='Pause', exact=True)).to_be_enabled()
    dialog.get_by_role('link', name='Back to tracks', exact=True).click()
    expect(dialog).to_be_hidden()


def test_superseded_album_play_does_not_open_artwork_for_a_stale_queue(album_app, page, monkeypatch):
    status = _player(page)
    page.goto(album_app.base + '/album?browse=MPRE_album')
    expect(page.locator('.hs-now-title')).to_have_text('Opening')
    dialog = page.locator('dialog.album-listening')
    expect(dialog).to_be_hidden()
    entered, release = threading.Event(), threading.Event()
    get_album = album_app.client.get_album

    def delayed_lookup(browse):
        entered.set()
        assert release.wait(8)
        return get_album(browse)

    monkeypatch.setattr(album_app.client, 'get_album', delayed_lookup)
    newer = 'https://music.youtube.com/watch?v=new-song'
    with page.expect_response('**/album/play') as response:
        page.get_by_role('button', name='Play Late Night', exact=True).click()
        try:
            assert entered.wait(4)
            status['now_playing']['playlist_ytm_id'] = 'ALBUM_QUEUE'
            page.evaluate('window.playbackStatus.refresh()')
            page.wait_for_function("window.playbackStatus.snapshot.now_playing.playlist_ytm_id === 'ALBUM_QUEUE'")
            assert page.request.post(album_app.base + '/play', data={'url': newer}).json()['ok']
        finally:
            release.set()
    assert response.value.status == 204
    assert 'hx-trigger' not in response.value.headers
    assert album_app.sent == [{'type': 'navigate', 'url': newer}]
    expect(dialog).to_be_hidden()
    expect(page.get_by_role('button', name='Show album artwork')).to_be_hidden()


def test_delayed_album_reply_cannot_open_artwork_after_a_newer_song(album_app, page):
    status = _player(page)
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.goto(album_app.base + '/album?browse=MPRE_album')
    dialog = page.locator('dialog.album-listening')
    expect(page.locator('.hs-now-title')).to_have_text('Opening')
    expect(dialog).to_be_hidden()
    newer = 'https://music.youtube.com/watch?v=new-song'

    def delay_play_response(route):
        completed = route.fetch()
        assert completed.status == 204 and 'album-play-started' in completed.headers['hx-trigger']
        status['now_playing']['playlist_ytm_id'] = 'ALBUM_QUEUE'
        page.evaluate('window.playbackStatus.refresh()')
        page.wait_for_function("window.playbackStatus.snapshot.now_playing.playlist_ytm_id === 'ALBUM_QUEUE'")
        assert page.evaluate('(url) => window.tcPlay(url)', newer)
        route.fulfill(response=completed)

    page.route('**/album/play', delay_play_response)
    button = page.get_by_role('button', name='Play Late Night', exact=True)
    with page.expect_response('**/album/play') as response:
        button.click()
    assert response.value.status == 204
    expect(page.locator('[hx-post="/album/play"]')).to_be_enabled()
    assert len(album_app.sent) == 2 and album_app.sent[-1]['url'] == newer
    expect(dialog).to_be_hidden()
    expect(page.get_by_role('button', name='Show album artwork')).to_be_hidden()


def test_private_album_play_opens_artwork_when_observed(album_app, page):
    status = _player(page)
    browse = UPLOAD_RELEASE_PREFIX + '_album'
    album_url = '/album?browse=' + browse
    page.goto(album_app.base + album_url)
    dialog = page.locator('dialog.album-listening')
    expect(dialog).to_be_hidden()
    with page.expect_response('**/play') as response:
        page.get_by_role('button', name='Play Private release', exact=True).click()
    assert response.value.json()['ok']
    assert album_app.sent == [{'type': 'navigate', 'url': 'https://music.youtube.com/browse/' + browse}]
    expect(dialog).to_be_hidden()
    status['now_playing'].update(album_browse_id=browse, playlist_ytm_id='',
                                 source={'kind': 'album', 'url': album_url, 'title': 'Private release'})
    page.evaluate('window.playbackStatus.refresh()')
    expect(page.get_by_role('dialog', name='Private release', exact=True)).to_be_visible()


@pytest.mark.parametrize('source', ['library', 'catalog'])
def test_search_album_play_opens_the_same_artwork_view(album_app, store, page, source):
    _player(page)['now_playing']['playlist_ytm_id'] = 'ALBUM_QUEUE'
    page.route('**/search/catalog?*', lambda route: route.fulfill(json={'songs': []}))
    page.route('**/search/catalog/sections?*', lambda route: route.fulfill(json={
        'artists': [], 'playlists': [], 'albums': [{'browse_id': 'MPRE_album', 'title': 'Late Night',
            'sub': 'The Evening Sessions', 'href': '/album?browse=MPRE_album',
            'thumbnail': '/album-cover.svg', 'play_url': 'https://music.youtube.com/browse/MPRE_album'}]}))
    store.tracks.upsert_track('one', 'Opening', 'The Evening Sessions', 'Late Night',
        album_browse_id='MPRE_album', thumbnail='/album-cover.svg')
    store.collection.add_saved_album({'browse': 'MPRE_album', 'title': 'Late Night',
        'artist': 'The Evening Sessions', 'thumbnail': '/album-cover.svg'})
    page.goto(album_app.base + '/playlists')
    page.keyboard.press('Control+k')
    search = page.locator('#music-search')
    search.get_by_role('searchbox', name='Search music').fill('evening sessions')
    if source == 'catalog':
        search.get_by_role('tab', name='YouTube Music', exact=True).click()
    play = search.get_by_role('link', name='Play Late Night', exact=True)
    with page.expect_response('**/album/play') as response:
        play.click()
    assert response.value.status == 204
    expect(page.get_by_role('dialog', name='Late Night', exact=True)).to_be_visible()
    assert len(album_app.sent) == 1
