"""TV ownership through the installed MV3 bridge and actual browser media.

Music's private APIs and the YouTube iframe API are controlled. Navigation,
application routes, extension worlds, WebSocket and media pause/play are real.
"""
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from tests.browser import test_extension_lifecycle_browser as lifecycle
from tests.browser.test_extension_lifecycle_browser import installed_bridge  # noqa: F401
from tests.browser.test_tv_browser import store, tv_ui  # noqa: F401
from tests.browser.test_tv_navigation_browser import start_tv


@pytest.fixture
def page(installed_bridge):
    page = installed_bridge.context.new_page()
    yield page
    page.close()


@pytest.fixture
def live_app(installed_bridge):
    return installed_bridge.worker.evaluate('TC_CONFIG.appOrigin')


@pytest.mark.parametrize('installed_bridge', ['full_app'], indirect=True)
def test_native_music_play_retires_tv_and_mix_play_pauses_the_retained_iframe(
        installed_bridge, page, live_app, tv_ui, store):
    h = installed_bridge
    page.set_viewport_size({'width': 1440, 'height': 1000})
    loaded = {}
    assets = {'tv.js', 'playback-status.js', 'playback-controls.js', 'player-card.js'}

    def asset(response):
        name = urlsplit(response.url).path.removeprefix('/static/')
        if name in assets:
            loaded[name] = hashlib.sha256(response.body()).hexdigest()

    page.on('response', asset)
    other = lifecycle._music(h, '?playing=true&v=other-song')
    h.eventually(lambda: (h.bridge.now_playing or {}).get('video_id') == 'other-song')
    page.unroute('**/home/tv/takeover')
    start_tv(page, live_app, store)
    assert other.locator('video').evaluate('(video) => video.paused')
    # Give the controlled iframe API real media in its retained browsing context.
    page.evaluate('''async () => {
        const p = window.tvPlayer, frame = window.originalTvFrame;
        const video = frame.contentDocument.createElement('video');
        video.src = 'https://music.youtube.com/fixture.wav'; video.loop = true;
        frame.contentDocument.body.append(video); window.embeddedMedia = video;
        const state = data => {p.state = data; p.options.events.onStateChange({data,target:p});};
        video.addEventListener('playing', () => state(1));
        video.addEventListener('pause', () => state(2));
        p.playVideo = () => {window.tvCalls.push(['play',p.id]); void video.play();};
        p.pauseVideo = () => {window.tvCalls.push(['pause',p.id]); video.pause();};
        p.getCurrentTime = () => video.currentTime;
        await video.play();
    }''')
    iid = store.accounts.get_accounts()[0].id
    pid = store.playlists.upsert_playlist(iid, 'PLMIX', 'More of what you love', 1, 'mix', 1)
    track = store.tracks.upsert_track('mix-song', 'First song', 'Recording artist', 'Album', 200, 1)
    store.playlists.set_playlist_tracks(pid, [track])
    page.evaluate('url => location.assign(url)', f'{live_app}/playlist/{pid}')
    expect(page.get_by_role('heading', name='More of what you love', exact=True)).to_be_visible()
    card = page.locator('.global-now-playing')
    expect(card.locator('.hs-now-title')).to_have_text('Video 0')
    expect(card.locator('.hs-source')).to_have_text('Back to TV')
    expect(card.get_by_role('button', name='Pause', exact=True)).to_be_visible()
    assert page.evaluate('!window.embeddedMedia.paused')
    # A muted Music tab cannot displace the TV that is actually playing.
    other.evaluate('document.querySelector("video").muted = true')
    other.get_by_role('button', name='Play or pause', exact=True).click()
    other.wait_for_function('!document.querySelector("video").paused')
    lifecycle._sample(h, other)
    h.eventually(lambda: (h.bridge.now_playing or {}).get('muted') is True)
    page.evaluate('window.playbackStatus.refresh()')
    expect(card.locator('.hs-now-title')).to_have_text('Video 0')
    assert page.evaluate('!window.embeddedMedia.paused && window.tvPlaybackControls.active()')
    # A real unmuted Play travels through MAIN, the worker and the server.
    other.evaluate('document.querySelector("video").muted = false')
    lifecycle._sample(h, other)
    h.eventually(lambda: (h.bridge.now_playing or {}).get('muted') is False)
    h.eventually(lambda: (h.bridge.now_playing or {}).get('paused') is False)
    page.evaluate('window.playbackStatus.refresh()')
    expect(card.locator('.hs-now-title')).to_have_text('First song')
    expect(card.locator('.hs-source')).to_have_count(0)
    page.wait_for_function('window.embeddedMedia.paused && !window.tvPlaybackControls.active()')
    assert page.evaluate('window.playbackStatus.progress().videoId') == 'other-song'
    card.get_by_role('button', name='Pause', exact=True).click()
    other.wait_for_function('document.querySelector("video").paused')
    # Navigation back does not launch TV. Deliberate Play pauses Music first.
    page.evaluate('url => location.assign(url)', live_app + '/#tv')
    expect(page.locator('.tv-player iframe')).to_be_visible()
    assert page.evaluate('window.embeddedMedia.paused')
    page.get_by_role('button', name='Now playing: Video 0', exact=True).click()
    page.wait_for_function('!window.embeddedMedia.paused && window.tvPlaybackControls.active()')
    assert other.locator('video').evaluate('(video) => video.paused')
    page.evaluate('url => location.assign(url)', f'{live_app}/playlist/{pid}')
    expect(page.get_by_role('heading', name='More of what you love', exact=True)).to_be_visible()
    page.get_by_role('button', name='Play on YouTube Music', exact=True).click()
    other.wait_for_url('**/playlist?list=PLMIX')
    # Only the fixture's selected queue/video is controlled; native controls and
    # the extension's observation/ownership selection remain production code.
    other.evaluate('''async () => {
        fixture.videoId = 'mix-song'; fixture.setBar(); fixture.setMedia();
        await document.querySelector('video').play(); fixture.sample();
    }''')
    h.eventually(lambda: (h.bridge.now_playing or {}).get('video_id') == 'mix-song')
    page.evaluate('window.playbackStatus.refresh()')
    expect(card.locator('.hs-now-title')).to_have_text('First song')
    expect(card.get_by_role('button', name='Pause', exact=True)).to_be_visible()
    expect(card.locator('.hs-source')).to_have_count(0)
    expect(page.get_by_role('button', name='Pause on YouTube Music', exact=True)).to_be_visible()
    assert page.evaluate('window.embeddedMedia.paused && !window.tvPlaybackControls.active()')
    assert page.evaluate('window.originalTvFrame === document.querySelector(".tv-player iframe")')
    assert page.evaluate('window.playbackStatus.progress().videoId') == 'mix-song'
    expect(page.locator('tr[data-vid="mix-song"]')).to_have_class(re.compile(r'\bnow-playing\b'))
    expect(page.locator('#playback-error, #toasts [role="alert"]')).to_have_count(0)
    root = Path(__file__).resolve().parents[2] / 'src/yt_playlist/web/static'
    assert loaded == {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in assets}
    page.screenshot(path='/tmp/tuneconsole-1153-installed-extension-handoff.png')
