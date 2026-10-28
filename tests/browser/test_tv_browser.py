"""TV's real preparation flow, with the external YouTube player boundary controlled."""
import pytest
from playwright.sync_api import expect

from tests.conftest import FakeClient
from tests.onboarding_support import publish_personal_mix
from yt_playlist.rec import tv, tv_artwork
from yt_playlist.util.matching import search_squash


@pytest.fixture
def tv_ui(store, live_app, page, monkeypatch):
    seeds = publish_personal_mix(store, now=1)
    for seed in seeds:
        store.rec_model.record_like(seed['key'], 1)
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')
    searches, api_loads, errors, created = [], [], [], []
    remote = {}
    # live_app supplies a fresh client per request. Keep remote playlist IDs
    # unique across those instances, just as the actual account does.
    def create(self, title, description):
        pid = f'PL_TV_{len(created)}'
        created.append(pid)
        self.created.append((pid, title, description))
        self._tracks[pid] = []
        remote[pid] = self._tracks[pid]
        return pid
    monkeypatch.setattr(FakeClient, 'create_playlist', create)
    original_read, original_add = FakeClient.get_playlist, FakeClient.add_playlist_items
    def read(self, pid, limit=None):
        return {'tracks': list(remote[pid])} if pid in remote else original_read(self, pid, limit)
    def add(self, pid, videos, **kwargs):
        self._tracks[pid] = remote[pid]
        self._catalog.update({t['video_id']: {'videoId': t['video_id'], 'title': t['title'],
            'artists': [{'name': t['artist']}]} for t in (tv.cached(store) or {}).get('items', [])})
        return original_add(self, pid, videos, **kwargs)
    monkeypatch.setattr(FakeClient, 'get_playlist', read)
    monkeypatch.setattr(FakeClient, 'add_playlist_items', add)
    monkeypatch.setattr(tv_artwork, 'prepare', lambda *args: False)
    def search(self, query, filter='songs'):
        searches.append((query, filter))
        return [{'videoId': f'visual{i:05}', 'title': f'Prepared {i} (Visualizer)',
                 'artists': [{'name': 'Artist'}], 'duration_seconds': 200,
                 'thumbnails': [{'url': live_app + '/static/favicon.svg', 'width': 100}]} for i in range(4)]
    monkeypatch.setattr(FakeClient, 'search', search)
    while tv.prepare(store, FakeClient(), 1):
        pass
    store.discovery.upsert_discovered_artist('Artist', 1, [], [], live_app + '/tv-artist.svg', 1)
    store.rec_proposals.put_proposals(tv_artwork.SURFACE, {
        search_squash('Artist'): {'images': [live_app + '/tv-artist.svg'], 'retry_at': 86401, 'at': 1}}, 1)
    searches.clear()
    page.route('**/tv-artist.svg', lambda route: route.fulfill(content_type='image/svg+xml', body='''
      <svg xmlns="http://www.w3.org/2000/svg" width="900" height="700"><defs><linearGradient id="a"><stop stop-color="#187a7b"/><stop offset="1" stop-color="#ba496c"/></linearGradient></defs><path fill="url(#a)" d="M0 0h900v700H0z"/><circle cx="450" cy="300" r="180" fill="#201a3e"/><path d="M160 700q290-440 580 0" fill="#201a3e"/></svg>'''))
    page.route('**/home/tv/takeover', lambda route: route.fulfill(json={'ok': True}))
    def player_api(route):
        api_loads.append(route.request.url)
        route.fulfill(content_type='application/javascript', body='''
          window.tvCalls = [];
          window.YT = {Player: class {
            constructor(mount, options) {
              this.options = options; this.id = options.videoId; this.position = 0; this.state = 2;
              const frame = document.createElement('iframe'); frame.title = 'YouTube video player';
              frame.srcdoc = '<p>Controlled YouTube player boundary</p>';
              mount.replaceWith(frame); this.frame = frame; window.tvPlayer = this;
              queueMicrotask(() => options.events.onReady({target:this}));
            }
            getCurrentTime() { return this.position; }
            getDuration() { return 200; }
            getPlayerState() { return this.state; }
            playVideo() {
              if (window.tvEndBeforePlay) {
                window.tvEndBeforePlay = false;
                this.state = 0; this.options.events.onStateChange({data:0,target:this});
              }
              this.state = 1; window.tvCalls.push(['play', this.id]); this.options.events.onStateChange({data:1,target:this});
            }
            pauseVideo() { this.state = 2; window.tvCalls.push(['pause', this.id]); this.options.events.onStateChange({data:2,target:this}); }
            loadVideoById(id) { this.id = id; this.position = 0; window.tvCalls.push(['load', id]); this.playVideo(); }
            destroy() { window.tvCalls.push(['destroy', this.id]); this.frame.remove(); }
          }};
          window.onYouTubeIframeAPIReady();
        ''')
    page.route('https://www.youtube.com/iframe_api', player_api)
    page.on('pageerror', lambda error: errors.append(str(error)))
    yield searches, api_loads
    assert not errors


def test_tv_ignores_ended_until_the_selected_video_has_started(page, live_app, tv_ui):
    page.goto(live_app + '/#tv')
    page.evaluate('window.tvEndBeforePlay = true')
    page.get_by_role('button', name='Play TV', exact=True).click()
    model = 'Alpine.$data(document.querySelector(".tv-home"))'
    page.wait_for_function(f'{model}.playing')
    expect(page.locator('.tv-film[aria-current="true"]')).to_have_attribute(
        'aria-label', 'Now playing: Prepared 0 (Visualizer)')
    assert page.evaluate('window.tvCalls') == [['play', 'visual00000']]

    # The same ordering can occur while loading another entry. A stale ended
    # notification must not skip that entry before its first PLAYING event.
    page.evaluate('window.tvEndBeforePlay = true; window.tvPlaybackControls.command("next")')
    page.wait_for_function(f'{model}.playing && {model}.selected === 1')
    expect(page.locator('.tv-film[aria-current="true"]')).to_have_attribute(
        'aria-label', 'Now playing: Prepared 1 (Visualizer)')
    assert page.evaluate('window.tvCalls.filter(call => call[0] === "load")') == [['load', 'visual00001']]

    # A real completion after playback still advances normally, including when
    # buffering was the last state reported before completion.
    page.evaluate('''window.tvPlayer.options.events.onStateChange({data:3,target:window.tvPlayer});
        window.tvPlayer.options.events.onStateChange({data:0,target:window.tvPlayer})''')
    page.wait_for_function(f'{model}.playing && {model}.selected === 2')
    assert page.evaluate('window.tvPlayer.id') == 'visual00002'


@pytest.mark.parametrize('code', [5, 150, 999])
def test_tv_retries_startup_errors_on_the_same_video_before_any_skip(page, live_app, tv_ui, code):
    page.goto(live_app + '/#tv')
    page.get_by_role('button', name='Play TV', exact=True).click()
    model = 'Alpine.$data(document.querySelector(".tv-home"))'
    page.wait_for_function(f'{model}.playing')
    page.evaluate('window.originalTvPlayer = window.tvPlayer; window.tvPlayer.position = 3.15')
    with page.expect_request(lambda request: request.url.endswith('/event')
                             and 'kind=player_error' in (request.post_data or '')) as event:
        page.evaluate('code => window.tvPlayer.options.events.onError({data:code,target:window.tvPlayer})', code)
    assert f'state={code}' in event.value.post_data
    assert 'position=3.15' in event.value.post_data
    page.wait_for_function(f'{model}.playing && window.tvCalls.some(call => call[0] === "load")')
    assert page.evaluate('window.tvPlayer === window.originalTvPlayer')
    assert page.evaluate(f'{model}.selected') == 0
    assert page.evaluate(f'{model}.failedVideos') == []
    assert page.evaluate('window.tvCalls.filter(call => call[0] === "load")') == [['load', 'visual00000']]
    expect(page.locator('.tv-film[aria-current="true"]')).to_have_attribute(
        'aria-label', 'Now playing: Prepared 0 (Visualizer)')

    page.evaluate('code => window.tvPlayer.options.events.onError({data:code,target:window.tvPlayer})', code)
    if code == 150:
        page.wait_for_function(f'{model}.playing && {model}.selected === 1')
        assert page.evaluate(f'{model}.failedVideos') == ['visual00000']
    else:
        expect(page.get_by_role('status').filter(has_text='This video could not play')).to_be_visible()
        assert page.evaluate(f'{model}.selected') == 0
        assert page.evaluate(f'{model}.failedVideos') == []
        assert not page.evaluate(f'{model}.playing')
        # An explicit Play gives the retained entry another attempt.
        page.evaluate('window.tvPlaybackControls.command("play")')
        page.wait_for_function(f'{model}.playing && !{model}.playerError')
        assert page.evaluate('window.tvCalls.filter(call => call[0] === "load")') == [
            ['load', 'visual00000'], ['load', 'visual00000']]


@pytest.mark.parametrize('width', [1440, 390])
def test_tv_uses_prepared_pool_without_preview_and_shared_controls_own_video(page, live_app, tv_ui, width):
    searches, api_loads = tv_ui
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app + '/')
    tabs = page.get_by_role('tab')
    labels = tabs.all_text_contents()
    assert labels.index('TV') == labels.index('Long play') + 1
    assert not searches and not api_loads
    page.get_by_role('tab', name='TV', exact=True).click()
    expect(page.get_by_role('button', name='Play TV', exact=True)).to_be_visible()
    expect(page.locator('.tv-home img, .tv-home ol')).to_have_count(0)
    assert not searches
    assert not api_loads
    expect(page.get_by_text('Up to 15 minutes per video', exact=True)).to_have_count(0)
    pending = []
    page.route('**/home/tv/play', lambda route: pending.append(route))
    page.get_by_role('button', name='Play TV', exact=True).click()
    expect(page.locator('.tv-loading')).to_be_visible()
    expect(page.locator('.tv-loading [data-loader-status]')).to_have_text('Generating TV session')
    expect(page.locator('.tv-loading .gen-node img')).to_have_count(0)
    expect(page.locator('.tv-filmstrip')).to_be_hidden()
    expect(page.get_by_text('Saving your video playlist…', exact=True)).to_have_count(0)
    page.screenshot(path=f'/tmp/tuneconsole-419-tv-loading-{width}.png')
    assert pending
    pending.pop().continue_()
    page.unroute('**/home/tv/play')
    expect(page.locator('.tv-player iframe')).to_be_visible()
    page.wait_for_function('window.tvCalls.some(call => call[0] === "play")')
    original_playlist = page.evaluate('Alpine.$data(document.querySelector(".tv-home")).playlistId')
    # The stream can already have prefetched beyond its initial saved draw.
    # Compare that draw, rather than the growing queue, when checking novelty.
    original_videos = ['visual00000', 'visual00001', 'visual00002']
    expect(page.locator('.tv-loading')).to_be_hidden()
    strip = page.get_by_role('navigation', name='TV playlist', exact=True)
    expect(strip.locator('button')).to_have_count(2)
    assert strip.inner_text().strip() == ''
    assert all(box['width'] <= 75 for box in (button.bounding_box() for button in strip.locator('button').all()))
    expect(strip.locator('[aria-current="true"]')).to_have_attribute('aria-label', 'Now playing: Prepared 0 (Visualizer)')
    expect(strip.get_by_role('button', name='Up next: Prepared 1 (Visualizer)', exact=True)).to_be_visible()
    expect(page.locator('body > .tv-backdrop')).to_be_visible()
    expect(page.locator('.tv-backdrop img')).to_have_attribute('src', live_app + '/tv-artist.svg')
    assert page.locator('.tv-backdrop').bounding_box() == {'x': 0, 'y': 0, 'width': width, 'height': 1000}
    assert len(api_loads) == 1
    frame = page.locator('.tv-player iframe').bounding_box()
    assert frame['width'] >= 200 and frame['height'] >= 200
    page.evaluate('window.tvPlayer.options.events.onStateChange({data:0,target:window.tvPlayer})')
    page.wait_for_function('window.tvCalls.some(call => call[0] === "load" && call[1] === "visual00001")')
    expect(strip.locator('button')).to_have_count(3)
    expect(strip.get_by_role('button', name='Up next: Prepared 2 (Visualizer)', exact=True)).to_be_visible()
    expect(page.locator('.global-now-playing .hs-now-title')).to_have_text('Prepared 1 (Visualizer)')
    page.locator('body').click(position={'x': 4, 'y': 4})
    page.keyboard.press('MediaPlayPause')
    page.wait_for_function('window.tvCalls.at(-1)[0] === "pause"')
    expect(page.locator('.tv-backdrop')).to_be_hidden()
    page.locator('.global-now-playing').get_by_role('button', name='Play', exact=True).click()
    page.wait_for_function('window.tvCalls.at(-1)[0] === "play"')
    page.locator('.global-now-playing').get_by_role('button', name='Next / skip', exact=True).click()
    page.wait_for_function('window.tvCalls.some(call => call[0] === "load" && call[1] === "visual00002")')
    page.screenshot(path=f'/tmp/tuneconsole-419-tv-{width}.png')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate("""[...document.querySelectorAll('body, main, #home, .home-dashboard, .dash-navigation, .dash-panel, .tv-home, .tv-theater, .tv-player, .tv-player iframe, .global-now-playing, .global-now-playing-slot')].map(el => [el.tagName,el.className,el.getBoundingClientRect().width,el.getBoundingClientRect().right,el.scrollWidth,el.clientWidth])""")
    continued = page.evaluate('({id: window.tvPlayer.id, calls: window.tvCalls.length})')
    page.get_by_role('tab', name='Mix tapes', exact=True).click()
    expect(page.locator('.tv-player iframe')).to_have_count(1)
    expect(page.locator('.tv-backdrop')).to_be_hidden()
    assert page.evaluate('({id: window.tvPlayer.id, calls: window.tvCalls.length})') == continued
    assert page.evaluate('window.tvPlaybackControls.active()')
    page.get_by_role('tab', name='TV', exact=True).click()
    expect(page.locator('.tv-player iframe')).to_be_visible()
    assert page.evaluate('Alpine.$data(document.querySelector(".tv-home")).playlistId') == original_playlist
    # Reset is the explicit action that requests a fresh draw.
    page.get_by_role('button', name='Adjust TV parameters').click()
    page.get_by_role('dialog', name='TV parameters').get_by_role('button', name='Reset playlist', exact=True).click()
    page.wait_for_function('original => Alpine.$data(document.querySelector(".tv-home")).playlistId !== original', arg=original_playlist)
    returned_videos = page.evaluate('Alpine.$data(document.querySelector(".tv-home")).items.map(item => item.video_id)')
    assert returned_videos[0] != original_videos[0]
    assert set(returned_videos) - set(original_videos)
    expect(page.locator('.tv-filmstrip button')).to_have_count(2)
    assert len(api_loads) == 1
    assert not searches
    page.goto(live_app + '/playlists')
    if page.locator('.playlist-shortcut').filter(has_text='Generated').is_visible():
        page.locator('.playlist-shortcut').filter(has_text='Generated').click()
    # The persisted Generated playlist routes playback back into TV in this tab.
    launch = page.locator(f'.pl-play[title="Play on TV"][href="/?tv_playlist={original_playlist}#tv"]:visible')
    expect(launch).to_have_attribute('target', '_self')
    launch.click()
    expect(page.locator('.tv-player iframe')).to_be_visible()
    expect(page.locator('.global-now-playing .hs-now-title')).to_have_text('Prepared 0 (Visualizer)')


def test_tv_parameters_apply_without_search_and_blocked_embeds_offer_recovery(page, live_app, tv_ui, store):
    page.goto(live_app + '/#tv')
    play = page.get_by_role('button', name='Play TV', exact=True)
    expect(play).to_be_visible()
    page.get_by_role('button', name='Adjust TV parameters').click()
    dialog = page.get_by_role('dialog', name='TV parameters')
    slider = dialog.get_by_role('slider', name='Maximum video length')
    expect(slider).to_have_value('9')
    slider.press('Home')
    expect(dialog.locator('output')).to_have_text('1 min')
    slider.press('ArrowRight')
    expect(dialog.locator('output')).to_have_text('2 min')
    dialog.get_by_role('button', name='Apply', exact=True).click()
    expect(dialog).to_be_hidden()
    expect(play).to_be_visible()
    expect(page.get_by_role('status').filter(has_text='No playable videos matched this length')).to_be_visible()
    page.get_by_role('button', name='Adjust TV parameters').click()
    slider.press('End')
    expect(dialog.locator('output')).to_have_text('Unlimited')
    page.screenshot(path='/tmp/tuneconsole-419-tv-length-slider.png')
    dialog.get_by_role('button', name='Apply', exact=True).click()
    expect(dialog).to_be_hidden()
    assert store.settings.get_setting(tv.SETTING) == 'unlimited'
    page.get_by_role('button', name='Adjust TV parameters').click()
    expect(slider).to_have_value('22')
    slider.press('Home')
    for _ in range(9):
        slider.press('ArrowRight')
    dialog.get_by_role('button', name='Apply', exact=True).click()
    expect(dialog).to_be_hidden()
    expect(play).to_be_visible()
    play.press('Enter')
    expect(page.locator('.tv-player iframe')).to_be_visible()
    for index in range(4):
        previous = page.evaluate('window.tvPlayer.id')
        loads = page.evaluate('window.tvCalls.filter(call => call[0] === "load").length')
        page.evaluate('window.tvPlayer.options.events.onError({data:150,target:window.tvPlayer})')
        page.wait_for_function('loads => window.tvCalls.filter(call => call[0] === "load").length > loads', arg=loads)
        assert page.evaluate('window.tvPlayer.id') == previous
        page.evaluate('window.tvPlayer.options.events.onError({data:150,target:window.tvPlayer})')
        if index < 3:
            page.wait_for_function('id => window.tvPlayer.id !== id', arg=previous)
    expect(page.get_by_role('status').filter(has_text='We couldn’t find the next video')).to_be_visible(timeout=10000)
    expect(page.get_by_role('button', name='Reset TV', exact=True)).to_be_visible()


@pytest.mark.parametrize('width', [1440, 390])
def test_reset_waits_inside_embed_until_background_pool_is_ready(page, live_app, tv_ui, store, width):
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(live_app + '/#tv')
    page.get_by_role('button', name='Play TV', exact=True).click()
    expect(page.locator('.tv-player iframe')).to_be_visible()
    original = page.evaluate('Alpine.$data(document.querySelector(".tv-home")).playlistId')
    store.rec_proposals.put_proposals(tv.SURFACE, {}, 1)
    page.get_by_role('button', name='Adjust TV parameters').click()
    with page.expect_response(lambda response: response.url.endswith('/home/tv/play') and response.status == 202):
        page.get_by_role('button', name='Reset playlist', exact=True).click()
    expect(page.locator('.tv-loading')).to_be_visible()
    expect(page.locator('.tv-loading [data-loader-status]')).to_have_text('Generating TV session')
    expect(page.locator('.tv-idle:visible, .tv-message:visible, .tv-filmstrip:visible')).to_have_count(0)
    assert 'TV is still being prepared' not in page.locator('.tv-home').inner_text()
    page.screenshot(path=f'/tmp/tuneconsole-419-tv-reset-wait-{width}.png')
    # Complete the real worker preparation. The pending reset proceeds itself;
    # no additional user action is needed and no preview is exposed meanwhile.
    while tv.prepare(store, FakeClient(), 1):
        pass
    page.wait_for_function('original => Alpine.$data(document.querySelector(".tv-home")).playlistId !== original && Alpine.$data(document.querySelector(".tv-home")).playing', arg=original)
    expect(page.locator('.tv-loading')).to_be_hidden()
    expect(page.locator('.tv-player iframe')).to_be_visible()


def test_initial_empty_pool_shows_the_embedded_loader(page, live_app, tv_ui, store):
    store.rec_proposals.put_proposals(tv.SURFACE, {}, 1)
    page.goto(live_app + '/#tv')
    expect(page.locator('.tv-loading')).to_be_visible()
    expect(page.locator('.tv-loading [data-loader-status]')).to_have_text('Generating TV session')
    expect(page.locator('.tv-idle')).to_be_hidden()
    expect(page.get_by_role('button', name='Play TV', exact=True)).to_be_hidden()
    expect(page.locator('.tv-filmstrip')).to_be_hidden()


def test_tv_waits_for_audio_pause_confirmation_and_rejects_failed_takeover(page, live_app, tv_ui):
    pending = []
    page.route('**/home/tv/takeover', lambda route: pending.append(route))
    page.goto(live_app + '/#tv')
    page.get_by_role('button', name='Play TV', exact=True).click()
    page.wait_for_function('window.tvPlayer')
    expect(page.locator('.tv-player iframe')).to_be_visible()
    page.wait_for_timeout(150)
    assert pending and page.evaluate('window.tvCalls') == []
    page.evaluate('window.tvPlaybackControls.release()')
    pending.pop().fulfill(json={'ok': True})
    page.wait_for_timeout(100)
    assert page.evaluate('window.tvCalls') == [['pause', 'visual00000']]
    assert not page.evaluate('window.tvPlaybackControls.active()')
    # The same mounted player can be resumed deliberately, but still requires
    # a fresh confirmed handoff before it plays.
    page.evaluate('void window.tvPlaybackControls.command("toggle")')
    page.wait_for_timeout(100)
    assert pending
    pending.pop().fulfill(status=409, json={'error': 'Audio pause was not confirmed'})
    expect(page.get_by_role('status').filter(has_text='Audio pause was not confirmed')).to_be_visible()
    assert page.evaluate('window.tvCalls') == [['pause', 'visual00000']]
    page.evaluate('void window.tvPlaybackControls.command("toggle")')
    page.wait_for_timeout(100)
    pending.pop().fulfill(json={'ok': True})
    page.wait_for_function('window.tvPlaybackControls.active() && window.tvCalls.at(-1)[0] === "play"')
