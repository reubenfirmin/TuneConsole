"""Continuous TV, keyboard transport, and actual backdrop image recovery."""
import pytest
from urllib.parse import parse_qs
from playwright.sync_api import expect

from tests.browser.test_tv_browser import store, tv_ui
from tests.test_tv_stream import video
from yt_playlist.rec import tv, tv_artwork
from yt_playlist.util.matching import search_squash
from tests.browser.quiet_action_support import (  # noqa: F401 - shared evidence fixture
    DIAGNOSTIC, action_evidence, assert_quiet_action, fail_action,
)


MODEL = 'Alpine.$data(document.querySelector(".tv-home"))'


def publish_pool(store, live_app, count):
    items = [{**video(i), 'thumbnail': live_app + '/static/favicon.svg'} for i in range(count)]
    store.rec_proposals.put_proposals(tv.SURFACE, {
        'schema': tv.SCHEMA, 'key': 'continuous', 'at': 1, 'attempted': [], 'items': items}, 1)
    store.rec_proposals.put_proposals(tv_artwork.SURFACE, {
        search_squash(item['artist']): {'images': [live_app + '/tv-artist.svg'], 'retry_at': 86401}
        for item in items}, 1)


def test_tv_failures_preserve_the_player_queue_draft_and_bounded_continuation(page, live_app, tv_ui, store, action_evidence):
    publish_pool(store, live_app, 7)
    starts, opens, parameters, resets, refills = [], [], [], [], []
    page.clock.install()
    page.goto(live_app + '/#tv')
    play = page.get_by_role('button', name='Play TV', exact=True)
    for kind in ('plaintext', 'malformed', 'network', 'json'):
        def failed_start(route, kind=kind):
            starts.append(route.request.url)
            fail_action(route, kind)
        page.route('**/home/tv/play', failed_start)
        play.click()
        expect(page.locator('.tv-message')).to_have_text('TV start not confirmed. Check your playlists before trying again.')
        expect(play).to_be_enabled()
        assert not page.evaluate(f'{MODEL}.playerMounted')
        assert_quiet_action(page)
        page.unroute('**/home/tv/play', failed_start)
    # A rejected fetch can carry an Error rather than a browser TypeError.
    # Exercise that actual catch with a diagnostic that must stay off screen.
    page.evaluate('''diagnostic => {
        window.actionOriginalFetch = window.fetch;
        window.fetch = (url, options) => String(url) === '/home/tv/play'
          ? Promise.reject(new Error(diagnostic)) : window.actionOriginalFetch(url, options);
    }''', DIAGNOSTIC)
    play.click()
    expect(page.locator('.tv-message')).to_have_text('TV start not confirmed. Check your playlists before trying again.')
    expect(play).to_be_enabled()
    assert_quiet_action(page)
    page.evaluate('() => { window.fetch = window.actionOriginalFetch; }')
    play.click()
    page.wait_for_function(f'{MODEL}.playing')
    page.evaluate('window.originalTvPlayer = window.tvPlayer; window.tvPlayer.position = 37')
    before = page.evaluate(f'''({{playlist: {MODEL}.playlistId,
        queue: {MODEL}.items.map(item => item.queue_id), calls: window.tvCalls}})''')
    for kind in ('plaintext', 'malformed', 'network', 'json'):
        def failed_open(route, kind=kind):
            opens.append(route.request.url)
            fail_action(route, kind)
        page.route('**/home/tv/playlist/999999', failed_open)
        page.evaluate(f'{MODEL}.openPlaylist("999999")')
        expect(page.locator('.tv-message')).to_have_text('Couldn’t open this video playlist. Choose another playlist or try again.')
        assert page.evaluate(f'{MODEL}.playing && window.tvPlayer === window.originalTvPlayer')
        assert page.evaluate('window.tvPlayer.position') == 37
        assert page.evaluate('window.tvCalls') == before['calls']
        assert_quiet_action(page)
        page.unroute('**/home/tv/playlist/999999', failed_open)
    page.get_by_role('button', name='Adjust TV parameters').click()
    dialog = page.get_by_role('dialog', name='TV parameters')
    slider = dialog.get_by_role('slider')
    slider.press('End')
    draft = slider.input_value()
    for kind in ('plaintext', 'malformed', 'network', 'json'):
        def failed_parameters(route, kind=kind):
            parameters.append(route.request.post_data)
            fail_action(route, kind)
        page.route('**/home/tv', failed_parameters)
        dialog.get_by_role('button', name='Apply', exact=True).click()
        expect(dialog.locator('p[role="status"]')).to_have_text('Changes not confirmed. Your choice is kept here.')
        expect(dialog.get_by_role('button', name='Apply', exact=True)).to_be_enabled()
        expect(slider).to_have_value(draft)
        assert page.evaluate(f'{MODEL}.playing && window.tvPlayer === window.originalTvPlayer')
        assert_quiet_action(page)
        page.unroute('**/home/tv', failed_parameters)
    def failed_reset(route):
        resets.append(route.request.url)
        fail_action(route, 'plaintext')
    page.route('**/home/tv/reset', failed_reset)
    dialog.get_by_role('button', name='Reset playlist', exact=True).click()
    expect(page.locator('.tv-message')).to_have_text('Reset not confirmed. Keep watching or try again.')
    assert page.evaluate(f'{MODEL}.playing && window.tvPlayer === window.originalTvPlayer')
    assert page.evaluate(f'{MODEL}.playlistId') == before['playlist']
    assert page.evaluate(f'{MODEL}.items.map(item => item.queue_id)') == before['queue']
    assert page.evaluate('window.tvCalls') == before['calls']
    assert len(starts) == len(opens) == len(parameters) == 4 and len(resets) == 1
    assert all(body == 'max_minutes=unlimited' for body in parameters)
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-retained.png', full_page=True)
    def failed_refill(route):
        refills.append(parse_qs(route.request.post_data))
        fail_action(route, 'plaintext')
    page.route('**/home/tv/playlist/*/refill', failed_refill)
    page.evaluate(f'{MODEL}.select(4)')
    for attempt in range(1, 4):
        if attempt > 1:
            page.clock.run_for(3000)
        page.wait_for_function(f'{MODEL}.refillAttempts === {attempt} && !{MODEL}.refilling')
    assert len(refills) == 3
    assert len({body['request_id'][0] for body in refills}) == 1
    assert page.evaluate(f'{MODEL}.playing && window.tvPlayer === window.originalTvPlayer')
    assert page.evaluate(f'{MODEL}.items.map(item => item.queue_id)') == before['queue']
    page.evaluate(f'{MODEL}.select(6)')
    page.evaluate('window.tvPlayer.options.events.onStateChange({data:0,target:window.tvPlayer})')
    expect(page.locator('.tv-message')).to_have_text('The next videos could not load. Reset TV to continue.')
    expect(page.get_by_role('button', name='Reset TV', exact=True)).to_be_enabled()
    page.clock.run_for(6000)
    assert len(refills) == 3 and len(resets) == 1 and len(starts) == 4
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-continuation.png', full_page=True)


def test_tv_keeps_playing_advancing_and_refilling_across_home_tabs(page, live_app, tv_ui, store):
    publish_pool(store, live_app, 7)
    pending = []
    takeovers = []
    def hold_takeover(route):
        takeovers.append(route)
    page.route('**/home/tv/takeover', hold_takeover)
    page.route('**/home/tv/playlist/*/refill', lambda route: pending.append(route))
    page.goto(live_app + '/#tv')
    dot = page.locator('.dash-tv-dot')
    tv_tab = page.get_by_role('tab', name='TV', exact=True)
    expect(dot).to_be_hidden()
    with page.expect_request('**/home/tv/takeover'):
        page.get_by_role('button', name='Play TV', exact=True).click()
    page.get_by_role('tab', name='Mix tapes', exact=True).click()
    expect(dot).to_be_hidden()  # waiting for the audio handoff is not playing
    takeovers.pop().fulfill(json={'ok': True})
    page.unroute('**/home/tv/takeover', hold_takeover)
    page.wait_for_function(f'{MODEL}.playing')
    playlist = page.evaluate(f'{MODEL}.playlistId')
    page.evaluate('window.originalTvPlayer = window.tvPlayer; window.tvPlayer.position = 37')
    calls = page.evaluate('window.tvCalls')
    for tab in ('On air', 'Mix tapes'):
        page.get_by_role('tab', name=tab, exact=True).click()
        assert page.evaluate(f'{MODEL}.playing && window.tvPlaybackControls.active()')
        assert page.evaluate('window.tvCalls') == calls
        expect(page.locator('.tv-backdrop')).to_be_hidden()
        expect(page.locator('.global-now-playing .hs-now-title')).to_have_text('Video 0')
        expect(dot).to_be_visible()
        expect(tv_tab).to_have_attribute('aria-description', 'TV is playing')
    controls = page.locator('.global-now-playing')
    expect(controls.locator('.hs-source')).to_have_text('Back to TV')
    expect(controls.get_by_role('button', name='Pause', exact=True)).to_be_in_viewport()
    # The short desktop rail must leave both playback and navigation usable.
    nav = page.locator('.topbar')
    road_trip = nav.get_by_role('link', name='Road Trip', exact=True)
    road_trip.scroll_into_view_if_needed()
    expect(road_trip).to_be_in_viewport()
    nav.get_by_role('button', name='Tools', exact=True).click()
    expect(nav.get_by_role('link', name='Setup', exact=True)).to_be_in_viewport()
    nav.get_by_role('button', name='Tools', exact=True).click()
    expect(controls.get_by_role('button', name='Pause', exact=True)).to_be_in_viewport()
    page.screenshot(path='/tmp/tuneconsole-tv-stream-controls.png')
    controls.get_by_role('button', name='Pause', exact=True).click()
    page.wait_for_function(f'!{MODEL}.playing')
    expect(dot).to_be_hidden()
    controls.get_by_role('button', name='Play', exact=True).click()
    page.wait_for_function(f'{MODEL}.playing')
    expect(dot).to_be_visible()
    assert page.evaluate('window.tvPlayer.position') == 37
    controls.get_by_role('button', name='Next / skip', exact=True).click()
    page.wait_for_function(f'window.tvPlayer.id === "visual00001" && {MODEL}.playing')
    for selected in (2, 3, 4):
        page.evaluate('window.tvPlayer.options.events.onStateChange({data:0,target:window.tvPlayer})')
        page.wait_for_function(f'window.tvPlayer.id === "visual0000{selected}" && {MODEL}.playing')
    page.wait_for_function(f'{MODEL}.refilling')
    assert len(pending) == 1
    publish_pool(store, live_app, 60)
    pending.pop().continue_()
    page.wait_for_function(f'{MODEL}.items.length > 7 && !{MODEL}.refilling')
    assert page.evaluate(f'{MODEL}.playlistId') == playlist
    controls.locator('.hs-source').click()
    expect(page.locator('.tv-player iframe')).to_be_visible()
    expect(page.locator('.tv-backdrop')).to_be_visible()
    expect(dot).to_be_hidden()
    assert page.evaluate('window.tvPlayer === window.originalTvPlayer')
    assert page.evaluate('window.tvPlayer.id') == 'visual00004'
    # Starting music elsewhere still releases TV, including while it is hidden.
    page.get_by_role('tab', name='Mix tapes', exact=True).click()
    page.evaluate('''window.dispatchEvent(new CustomEvent("playback-requested", {
        detail: {url: "https://music.youtube.com/watch?v=visual00059", requestId: 1}
    }))''')
    page.wait_for_function(f'!{MODEL}.playing && !window.tvPlaybackControls.active()')
    expect(dot).to_be_hidden()
    # Delayed PLAYING callbacks from the hidden iframe cannot take audio back.
    page.evaluate('window.tvPlayer.playVideo()')
    assert not page.evaluate(f'{MODEL}.playing || window.tvPlaybackControls.active()')
    page.get_by_role('tab', name='TV', exact=True).click()
    assert not page.evaluate(f'{MODEL}.playing || window.tvPlaybackControls.active()')
    assert page.evaluate('window.tvPlayer === window.originalTvPlayer')


def test_tv_continues_across_many_batches_with_bounded_history(page, live_app, tv_ui, store, record_property):
    publish_pool(store, live_app, 7)
    batches, ended, playing = [], [], []
    def record_response(response):
        if response.url.endswith('/refill'):
            batches.append(response)
        elif response.url.endswith('/event'):
            body = response.request.post_data or ''
            if body.startswith('kind=ended&'):
                ended.append(response)
            elif body.startswith('kind=state&state=playing&'):
                playing.append(response)
    page.on('response', record_response)
    page.goto(live_app + '/#tv')
    page.get_by_role('button', name='Play TV', exact=True).click()
    page.wait_for_function(f'{MODEL}.playing')
    assert page.evaluate(f'{MODEL}.items.length') == 7
    publish_pool(store, live_app, 60)
    played = []
    for _ in range(75):
        previous = page.evaluate(f'''() => {{
            const previous = {{queue: {MODEL}.current.queue_id, video: window.tvPlayer.id}};
            window.tvPlayer.options.events.onStateChange({{data:0,target:window.tvPlayer}});
            return previous;
        }}''')
        played.append(previous['video'])
        # Capture both bounds when the real player has advanced, instead of
        # making separate browser reads after every playback transition.
        state = page.wait_for_function(f'''id => {{
            const model = {MODEL};
            return model.current.queue_id !== id && model.playing && {{
                count: model.items.length, revealed: model.revealedCount, selected: model.selected
            }};
        }}''', arg=previous['queue']).json_value()
        assert state['count'] <= 30
        assert state['revealed'] <= state['selected'] + 2
    # Playback reports are queued separately from the player. Await their real
    # responses so closing a faster browser loop cannot discard its history.
    if len(ended) < 75 or len(playing) < 76:
        page.wait_for_event('response', predicate=lambda _: len(ended) >= 75 and len(playing) >= 76)
    assert len(ended) == 75 and all(response.status == 200 for response in ended)
    assert len(playing) == 76 and all(response.status == 200 for response in playing)
    assert len(set(played)) > 7
    assert all(a != b for a, b in zip(played, played[1:]))
    refills = len([response for response in batches if response.status == 200])
    record_property('tv_transitions', len(played))
    record_property('tv_successful_refills', refills)
    record_property('tv_completed_ended_reports', len(ended))
    assert refills >= 15
    assert page.locator('.tv-filmstrip').inner_text().strip() == ''
    assert all(len(store.charts.playlist_tracks_detail(p.id)) <= 60
               for p in store.playlists.get_playlists())


def test_tv_waits_at_batch_end_and_recovers_when_prepared_videos_arrive(page, live_app, tv_ui, store):
    publish_pool(store, live_app, 7)
    page.goto(live_app + '/#tv')
    page.get_by_role('button', name='Play TV', exact=True).click()
    page.wait_for_function(f'{MODEL}.playing')
    store.rec_proposals.put_proposals(tv.SURFACE, {}, 1)
    for _ in range(6):
        current = page.evaluate(f'{MODEL}.current.queue_id')
        page.evaluate('window.tvPlayer.options.events.onStateChange({data:0,target:window.tvPlayer})')
        page.wait_for_function(f'id => {MODEL}.current.queue_id !== id && {MODEL}.playing', arg=current)
    with page.expect_response(lambda response: response.url.endswith('/refill') and response.status == 202):
        page.evaluate('window.tvPlayer.options.events.onStateChange({data:0,target:window.tvPlayer})')
    expect(page.locator('.tv-loading')).to_be_visible()
    assert page.evaluate(f'{MODEL}.waiting')
    publish_pool(store, live_app, 60)
    page.wait_for_function(f'{MODEL}.playing && !{MODEL}.waiting', timeout=10000)
    expect(page.locator('.tv-loading')).to_be_hidden()
    assert page.evaluate('window.tvPlayer.id') not in [f'visual{i:05}' for i in range(7)]


def test_space_resumes_the_same_video_and_native_controls_keep_their_behavior(page, live_app, tv_ui):
    page.goto(live_app + '/#tv')
    page.get_by_role('button', name='Play TV', exact=True).click()
    page.wait_for_function(f'{MODEL}.playing')
    page.evaluate('window.tvPlayer.position = 37')
    before = page.evaluate(f'({{id: window.tvPlayer.id, playlist: {MODEL}.playlistId, entry: {MODEL}.current.queue_id}})')
    page.locator('body').click(position={'x': 4, 'y': 4})
    for playing in (False, True):
        page.keyboard.press('Space')
        page.wait_for_function(f'{MODEL}.playing === {str(playing).lower()}')
        assert page.evaluate('window.tvPlayer.position') == 37
    thumbnail = page.locator('.tv-film[aria-current="true"]')
    thumbnail.focus()
    for playing in (False, True):
        page.keyboard.press('Space')
        page.wait_for_function(f'{MODEL}.playing === {str(playing).lower()}')
        assert page.evaluate('window.tvPlayer.position') == 37
    after = page.evaluate(f'({{id: window.tvPlayer.id, playlist: {MODEL}.playlistId, entry: {MODEL}.current.queue_id}})')
    assert after == before
    assert not page.evaluate('window.tvCalls.some(call => call[0] === "load")')
    page.evaluate("document.body.insertAdjacentHTML('beforeend', '<input id=tv-keyboard-test aria-label=Editing>')")
    page.get_by_role('textbox', name='Editing', exact=True).fill('typing')
    page.keyboard.press('Space')
    expect(page.get_by_role('textbox', name='Editing', exact=True)).to_have_value('typing ')
    assert page.evaluate(f'{MODEL}.playing')
    # Release an already paused iframe. Its first native Play must reacquire
    # ownership even when the player sends no second PAUSED acknowledgement.
    page.evaluate(f'{MODEL}.controls.command("pause")')
    page.evaluate('window.tvPlayer.pauseVideo = function () { this.state = 2; }; window.tvPlaybackControls.release()')
    page.evaluate('window.tvPlayer.playVideo()')
    page.wait_for_function(f'{MODEL}.playing && window.tvPlaybackControls.active()')
    assert page.evaluate('window.tvPlayer.position') == 37


@pytest.mark.parametrize('width', [1792, 1440])
def test_artist_photo_covers_viewport_and_uses_alternatives_before_video(page, live_app, tv_ui, store, width):
    page.set_viewport_size({'width': width, 'height': 1000})
    page.route('**/broken-artist.jpg', lambda route: route.fulfill(status=404))
    store.rec_proposals.put_proposals(tv_artwork.SURFACE, {
        search_squash('Artist'): {'images': [live_app + '/broken-artist.jpg', live_app + '/tv-artist.svg'],
                                'retry_at': 86401}}, 1)
    # Remove the discovery thumbnail, so the deliberately broken candidate is
    # actually attempted before the second artist photo.
    with store.conn:
        store.conn.execute('UPDATE discovered_artists SET thumbnail=NULL')
    page.goto(live_app + '/#tv')
    page.get_by_role('button', name='Play TV', exact=True).click()
    page.wait_for_function(f'{MODEL}.playing')
    photo = page.locator('body > .tv-backdrop img')
    expect(photo).to_have_attribute('src', live_app + '/tv-artist.svg')
    page.wait_for_function('document.querySelector(".tv-backdrop img")?.naturalWidth > 0')
    assert page.evaluate(f'{MODEL}.failedArt.includes({(repr(live_app + "/broken-artist.jpg"))})')
    bounds = photo.bounding_box()
    assert bounds['x'] <= 0 and bounds['x'] + bounds['width'] >= width
    assert bounds['y'] <= 0 and bounds['y'] + bounds['height'] >= 1000
    page.screenshot(path=f'/tmp/tuneconsole-419-artwork-{width}.png')
    page.evaluate(f'{MODEL}.artFailed({repr(live_app + "/tv-artist.svg")})')
    expect(photo).to_have_attribute('src', live_app + '/static/favicon.svg')
    assert page.evaluate(f'{MODEL}.playing')


def test_late_refill_cannot_resume_after_audio_takes_ownership(page, live_app, tv_ui, store):
    publish_pool(store, live_app, 7)
    pending = []
    page.route('**/home/tv/playlist/*/refill', lambda route: pending.append(route))
    page.goto(live_app + '/#tv')
    page.get_by_role('button', name='Play TV', exact=True).click()
    page.wait_for_function(f'{MODEL}.playing')
    for _ in range(4):
        current = page.evaluate(f'{MODEL}.current.queue_id')
        page.evaluate('window.tvPlayer.options.events.onStateChange({data:0,target:window.tvPlayer})')
        page.wait_for_function(f'id => {MODEL}.current.queue_id !== id && {MODEL}.playing', arg=current)
    page.wait_for_function(f'{MODEL}.refilling')
    before = page.evaluate(f'{MODEL}.items.length')
    page.evaluate('window.tvPlaybackControls.release()')
    pending.pop().fulfill(json={'playlist_id': 999, 'stream': 'stale', 'title': 'Stale',
                                'items': [{**video(59), 'thumbnail': live_app + '/static/favicon.svg'}]})
    page.wait_for_function(f'!{MODEL}.refilling')
    assert page.evaluate(f'{MODEL}.items.length') == before
    assert not page.evaluate('window.tvPlaybackControls.active()')
    assert not page.evaluate(f'{MODEL}.playing')
