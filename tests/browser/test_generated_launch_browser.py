"""Real save requests and popup lifecycle, with a controlled provider/player."""
import base64
from pathlib import Path
import re
import time
from urllib.parse import quote

import pytest
from playwright.sync_api import expect

from tests.conftest import FakeClient, _track
from yt_playlist.library.sync import sync_identity


@pytest.fixture
def generation_app(request, monkeypatch, store, page):
    from yt_playlist.web import app as app_module

    original, apps = app_module.create_app, []

    def capture(*args, **kwargs):
        app = original(*args, **kwargs)
        apps.append(app)
        return app

    monkeypatch.setattr(app_module, 'create_app', capture)
    base = request.getfixturevalue('live_app')
    ctx = apps[0].state.ctx
    account = store.accounts.get_master_account().id
    provider = FakeClient(playlists=[{'playlistId': 'SOURCE', 'title': 'Ready', 'count': 2}],
                          tracks={'SOURCE': [_track('one', 'First song', 'Artist'),
                                             _track('two', 'Second song', 'Artist')]})
    sync_identity(store, account, provider, now=1)
    ctx.client_provider = lambda: {account: provider}
    store.settings.set_setting('onboard_listening_format', 'mixes')
    store.settings.set_setting('onboard_format_pending', '0')
    bridge, frames = ctx.bridge, []
    bridge._send = lambda frame: None

    def navigate(frame, *, intent=None):
        if intent is not None and not bridge.playback_intent_current(intent):
            return False
        frames.append(frame)
        playlist = frame['url'].split('list=')[-1]
        bridge.now_playing = {'playlist_ytm_id': playlist, 'video_id': 'one', 'paused': False,
                              'title': 'First song', 'artist': 'Artist'}
        bridge.now_playing_seen_at = time.monotonic()
        return True

    monkeypatch.setattr(bridge, 'send_control', navigate)
    if getattr(request, 'param', None) == 'board':
        from tests.test_home_other_genres import seed_genres
        from tests.test_mode_cards_route import prepare_home

        from yt_playlist.rec import account_growth
        growth_view = account_growth.view

        def unlocked_mixes(*args, **kwargs):
            result = growth_view(*args, **kwargs)
            result['ready_formats']['mixes'] = True
            result['ready_areas']['directions'] = True
            return result

        monkeypatch.setattr(account_growth, 'view', unlocked_mixes)
        provider._catalog['v'] = _track('v', 'Prepared song', 'Artist')
        # Seed before the browser starts polling the shared in-memory fixture.
        seed_genres(store)
        store.settings.set_setting('last_sync_at', '1')
        store.settings.set_setting('onboard_dismissed', '1')
        store.settings.set_setting('onboard_welcomed', '1')
        store.settings.set_setting('home_format', 'mixes')
        prepare_home(store, 1)
        for track in store.conn.execute('SELECT video_id,title,artist,duration_s FROM catalog_tracks WHERE video_id IS NOT NULL'):
            provider._catalog[track['video_id']] = _track(track['video_id'], track['title'], track['artist'],
                track['duration_s'] or 200, set_video_id='set-' + track['video_id'])
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.goto(base)
    return base, provider, bridge, frames


def test_save_closes_its_popup_and_confirms_actual_player(generation_app, page):
    base, provider, bridge, frames = generation_app
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    with page.expect_popup() as opened:
        page.get_by_role('button', name='Save and play this mix').click()
    popup = opened.value
    page.wait_for_url(re.compile(r'/playlist/\d+$'))
    assert popup.is_closed()
    page.evaluate('window.playbackStatus.refresh()')
    expect(page.locator('[data-generated-playback]')).to_have_text('Playing your saved mix on YouTube Music.')
    assert len(provider.created) == len(frames) == 1
    assert provider.created[0][0] in frames[0]['url']
    assert not errors


@pytest.mark.parametrize('generation_app', ['board'], indirect=True)
def test_focused_four_mix_board_saves_and_launches_once(generation_app, page):
    base, provider, bridge, frames = generation_app
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(base)
    card = page.locator('#gen-wheelhouse')
    expect(card).to_be_visible()
    card.locator('.gen-poster').click()
    expect(card).to_have_class(re.compile('is-focused'))
    with page.expect_popup() as opened:
        card.get_by_role('button', name='Save and play this mix').click()
    page.wait_for_url(re.compile(r'/playlist/\d+$'))
    assert opened.value.is_closed()
    page.evaluate('window.playbackStatus.refresh()')
    expect(page.locator('[data-generated-playback]')).to_contain_text('Playing your saved mix')
    assert len(provider.created) == len(frames) == 1
    assert not errors


@pytest.mark.parametrize('generation_app', ['board'], indirect=True)
@pytest.mark.parametrize('refresh', ['cards', 'sync'])
def test_background_refresh_does_not_replace_a_saving_mix(generation_app, page, refresh):
    base, provider, bridge, frames = generation_app
    pending, errors = {}, []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def hold_refresh(route):
        pending['refresh_response'] = route.fetch()
        pending['refresh'] = route

    url = base + ('/home/cards?prepare_attempt=1' if refresh == 'cards' else '/')
    page.route(url, hold_refresh)
    # The refresh is already in flight when the user selects and plays a mix.
    page.evaluate("""({url, refresh}) => { htmx.ajax('GET', url, {
        target: refresh === 'cards' ? '#home-mode-cards' : '#home',
        select: refresh === 'cards' ? '#home-mode-cards' : '#home', swap: 'outerHTML',
        headers: {'X-Home-Refresh': 'sync'} }); }""", {'url': url, 'refresh': refresh})
    deadline = time.monotonic() + 10
    while 'refresh' not in pending and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert 'refresh' in pending

    def hold_save(route):
        pending['save_response'] = route.fetch()
        pending['save'] = route

    page.route('**/home/generate', hold_save)
    card = page.locator('#gen-wheelhouse')
    card.locator('.gen-poster').click()
    with page.expect_popup() as opened:
        card.get_by_role('button', name='Save and play this mix').click()
    deadline = time.monotonic() + 10
    while 'save' not in pending and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert 'save' in pending
    pending['refresh'].fulfill(response=pending['refresh_response'])
    expect(card).to_have_class(re.compile('is-focused'))
    expect(card.locator('.gen-play')).to_be_disabled()
    pending['save'].fulfill(response=pending['save_response'])
    page.wait_for_url(re.compile(r'/playlist/\d+$'))
    assert opened.value.is_closed()
    assert len(provider.created) == len(frames) == 1
    assert not errors


@pytest.mark.parametrize('previous_key_format', [False, True])
def test_lost_save_response_is_terminal_and_does_not_create_again(generation_app, page, previous_key_format):
    base, provider, bridge, frames = generation_app
    requests = []

    def lose_response(route):
        requests.append(route.request)
        # The remote write really finishes; only its response to this browser
        # is lost. A retry must not create another copy of that saved playlist.
        response = route.fetch()
        assert response.status == 200
        route.abort('failed')

    page.route('**/home/generate', lose_response)
    with page.expect_popup() as opened:
        page.get_by_role('button', name='Save and play this mix').click()
    popup = opened.value
    expect(page.locator('[data-generation-error]')).to_contain_text('couldn’t confirm playback')
    expect(popup.get_by_role('alert')).to_contain_text('couldn’t confirm playback')
    expect(popup.get_by_role('heading')).to_have_text(page.locator('.gen-card-body').get_attribute('data-name'))
    expect(popup.get_by_role('button', name='Back to this mix')).to_be_visible()
    play = page.get_by_role('button', name='Save and play this mix')
    expect(play).to_be_disabled()
    # A script-triggered request and a reload also retain the uncertain save.
    play.evaluate("el => htmx.trigger(el, 'click')")
    original_order = [row.get_attribute('data-vid') for row in page.locator('.starter-mix [data-vid]').all()]
    assert len(original_order) == 2
    if previous_key_format:
        # Simulate the browser retaining a save from before the key stopped
        # depending on sampler order. Updating the UI cannot authorize a retry.
        page.evaluate("""() => {
            const key = Object.keys(sessionStorage).find(key => key.startsWith('tc-generation:'));
            const prefix = key.slice(0, key.indexOf('['));
            const videos = JSON.parse(key.slice(prefix.length)).sort().reverse();
            const token = sessionStorage.getItem(key);
            sessionStorage.removeItem(key);
            sessionStorage.setItem(prefix + JSON.stringify(videos), token);
        }""")
    for _ in range(16):
        page.reload()
        current_order = [row.get_attribute('data-vid') for row in page.locator('.starter-mix [data-vid]').all()]
        if current_order != original_order:
            break
    assert current_order != original_order, 'Exercise the real sampler changing order on reload'
    play.click()
    expect(play).to_be_disabled()
    assert len(provider.created) == len(requests) == 1
    page.screenshot(path='/tmp/tuneconsole-634-save-unknown.png', full_page=True)
    popup.screenshot(path='/tmp/tuneconsole-634-popup-failure.png')


@pytest.mark.parametrize('generation_app', ['board'], indirect=True)
@pytest.mark.parametrize('refresh', ['cards', 'sync'])
def test_background_refresh_resumes_after_leaving_a_mix(generation_app, page, refresh):
    base, *_ = generation_app
    card = page.locator('#gen-wheelhouse')
    card.locator('.gen-poster').click()
    before = page.locator('#home-mode-cards').element_handle()
    page.evaluate("""refresh => {
        if (refresh === 'sync') {
            const status = Alpine.$data(document.getElementById('home'));
            status._job = 'completed-fixture-sync';
            status.finishJob('completed-fixture-sync', {outcome: 'complete'});
        } else {
            // This fixture has fully prepared cards. Install the same real HTMX
            // polling contract observed on the native still-preparing board.
            const board = document.getElementById('home-mode-cards');
            board.setAttribute('hx-get', '/home/cards?prepare_attempt=1');
            board.setAttribute('hx-trigger', 'every 3s');
            board.setAttribute('hx-swap', 'outerHTML');
            htmx.process(board);
        }
    }""", refresh)
    page.wait_for_timeout(3300)
    assert before.evaluate('node => node.isConnected')
    expect(card).to_have_class(re.compile('is-focused'))
    card.locator('.gen-back').click()
    # The existing three-second scans/poll retry after the user closes the card.
    page.wait_for_function('node => !node.isConnected', arg=before, timeout=12000)
    expect(page.locator('#gen-wheelhouse')).to_be_visible()


def test_saved_mix_reports_disconnected_player_without_resaving(generation_app, page):
    base, provider, bridge, frames = generation_app
    create = provider.create_playlist

    def disconnect_during_create(*args, **kwargs):
        bridge._send = None
        return create(*args, **kwargs)

    provider.create_playlist = disconnect_during_create
    with page.expect_popup():
        page.get_by_role('button', name='Save and play this mix').click()
    page.wait_for_url(re.compile(r'/playlist/\d+$'))
    page.evaluate('window.playbackStatus.refresh()')
    note = page.locator('[data-generated-playback]')
    expect(note).to_contain_text('Reconnect the YouTube Music extension')
    assert len(provider.created) == 1 and not frames
    page.screenshot(path='/tmp/tuneconsole-634-playback-disconnected.png', full_page=True)


@pytest.mark.parametrize('generation_app', ['board'], indirect=True)
def test_unconfirmed_provider_create_cannot_be_repeated_after_reload(generation_app, page):
    base, provider, bridge, frames = generation_app
    page.unroute('https://i.ytimg.com/**')
    page.route('https://i.ytimg.com/**', lambda route: route.fulfill(
        content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300">'
        '<path fill="#503b69" d="M0 0h300v300H0z"/><circle cx="150" cy="150" r="90" fill="#a9cbbd"/></svg>'))
    card = page.locator('#gen-wheelhouse')
    card.locator('.gen-poster').click()
    count = card.locator('.gen-row').count()
    card.get_by_role('button', name='Remove from this playlist').first.click()
    expect(card.locator('.gen-row')).to_have_count(count - 1)
    selected = card.locator('.gen-row').evaluate_all(
        "rows => rows.map(row => ({title:row.dataset.title, artist:row.dataset.artist, video:row.dataset.vid}))")
    create = provider.create_playlist

    def applied_without_reply(*args, **kwargs):
        from yt_playlist.core.bridge import BridgeError
        create(*args, **kwargs)
        raise BridgeError('create reply timed out')

    provider.create_playlist = applied_without_reply
    with page.expect_popup() as opened:
        card.get_by_role('button', name='Save and play this mix').click()
    expect(page.locator('.gen-done')).to_contain_text('Playback unconfirmed')
    if not opened.value.is_closed():
        opened.value.wait_for_event('close', timeout=5000)
    assert opened.value.is_closed()
    expect(page.locator('.gen-done')).to_contain_text('We couldn’t confirm playback.')
    songs = page.get_by_role('list', name='Your selected songs')
    for index, track in enumerate(selected):
        row = songs.get_by_role('listitem').nth(index)
        expect(row).to_contain_text(track['title'])
        expect(row).to_contain_text(track['artist'])
        expect(row.get_by_role('link')).to_have_attribute('href', 'https://music.youtube.com/watch?v=' + quote(track['video']))
    assert songs.get_by_role('listitem').count() == len(selected)
    page.wait_for_function("""() => [...document.querySelectorAll('.gen-recovery-tracks img')].every(
        image => image.complete && image.naturalWidth > 0)""")
    page.screenshot(path='/tmp/tuneconsole-634-provider-uncertain.png', full_page=True)
    expect(page.locator('.gen-done').get_by_role('link', name='Find this mix on YouTube Music')).to_have_attribute(
        'href', re.compile('https://music.youtube.com/search\\?q='))
    assert len(provider.created) == 1 and not frames
    page.reload()
    card = page.locator('#gen-wheelhouse')
    expect(card.locator('.gen-row')).to_have_count(len(selected))
    assert card.locator('.gen-row').evaluate_all(
        "rows => rows.map(row => ({title:row.dataset.title, artist:row.dataset.artist, video:row.dataset.vid}))") == selected
    expect(card.get_by_role('button', name='Save and play this mix')).to_be_disabled()
    expect(card.locator('[data-generation-error]')).to_be_visible()
    assert len(provider.created) == 1


def test_paused_storage_can_open_saved_mix_without_a_local_playlist(generation_app, store, page):
    base, provider, bridge, frames = generation_app
    create = provider.create_playlist

    def disconnect_during_create(*args, **kwargs):
        bridge._send = None
        return create(*args, **kwargs)

    provider.create_playlist = disconnect_during_create
    store.activity_paused = True
    try:
        with page.expect_popup() as opened:
            page.get_by_role('button', name='Save and play this mix').click()
        expect(page.locator('.gen-done')).to_be_visible()
        deadline = time.monotonic() + 5
        while not opened.value.is_closed() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert opened.value.is_closed()
        page.evaluate('window.playbackStatus.refresh()')
        note = page.locator('[data-generated-playback]')
        expect(note).to_contain_text('Reconnect the YouTube Music extension')
        expect(note).not_to_contain_text('Use Play')
        expect(note.get_by_role('link')).to_have_attribute('href', re.compile('watch\\?list='))
        assert len(provider.created) == 1 and not frames
        assert len(store.playlists.get_playlists()) == 1
    finally:
        store.activity_paused = False


def test_playback_timeout_retries_the_saved_playlist(generation_app, page, monkeypatch):
    base, provider, bridge, frames = generation_app
    navigate = bridge.send_control
    # The first command is accepted but never reaches the native player.
    monkeypatch.setattr(bridge, 'send_control', lambda *args, **kwargs: True)
    with page.expect_popup():
        page.get_by_role('button', name='Save and play this mix').click()
    page.wait_for_url(re.compile(r'/playlist/\d+$'))
    page.clock.install()
    page.evaluate('window.playbackStatus.refresh()')
    note = page.locator('[data-generated-playback]')
    expect(note).to_contain_text('Waiting for YouTube Music playback')
    page.clock.fast_forward(46000)
    expect(note).to_contain_text('Playback has not started')
    monkeypatch.setattr(bridge, 'send_control', navigate)
    page.get_by_role('button', name='Play on YouTube Music', exact=True).click()
    page.evaluate('window.playbackStatus.refresh()')
    expect(note).to_have_text('Playing your saved mix on YouTube Music.')
    assert len(provider.created) == len(frames) == 1
    assert provider.created[0][0] in frames[0]['url']


def test_interstitial_without_a_response_exits_loading(generation_app, page):
    base, *_ = generation_app
    page.clock.install()
    page.goto(base + '/home/generating')
    expect(page.locator('.gen-loader')).to_be_visible()
    page.clock.fast_forward(90001)
    expect(page.get_by_role('alert')).to_contain_text('couldn’t confirm playback')
    expect(page.locator('.gen-loader')).to_be_hidden()


def test_popup_has_feedback_before_the_backend_answers(generation_app, page):
    base, provider, *_ = generation_app
    requests = []
    page.context.route('**/home/generating?*', lambda route: requests.append(route))
    page.route('**/home/generate', lambda route: requests.append(route))
    page.clock.install()
    with page.expect_popup() as opened:
        page.get_by_role('button', name='Save and play this mix').click()
    popup = opened.value
    # Locators wait for the pending navigation. Inspect the still-rendered
    # initial document through its opener while the server has sent no bytes.
    assert 'Preparing your mix' in page.evaluate(
        "window.__ytTab.document.querySelector('[role=status]').innerText")
    # Capture the actual compositor without Playwright waiting for navigation.
    cdp = page.context.new_cdp_session(popup)
    try:
        screenshot = cdp.send('Page.captureScreenshot')['data']
        Path('/tmp/tuneconsole-634-popup-before-response.png').write_bytes(base64.b64decode(screenshot))
    finally:
        cdp.detach()
    assert not provider.created
    page.clock.fast_forward(90001)
    expect(popup.get_by_role('alert')).to_contain_text('couldn’t confirm playback')
    expect(page.get_by_role('button', name='Save and play this mix')).to_be_disabled()
    page.screenshot(path='/tmp/tuneconsole-634-held-save.png', full_page=True)
    popup.screenshot(path='/tmp/tuneconsole-634-held-popup.png')


@pytest.mark.parametrize('asset', ['generating', 'theme'])
def test_interstitial_timeout_survives_unavailable_animation(generation_app, page, asset):
    base, *_ = generation_app
    page.route(f'**/static/{asset}.js?*', lambda route: route.abort('failed'))
    page.clock.install()
    page.goto(base + '/home/generating')
    page.clock.fast_forward(90001)
    expect(page.get_by_role('alert')).to_contain_text('couldn’t confirm playback')
    expect(page.locator('.gen-loader')).to_be_hidden()


def test_interstitial_timeout_survives_a_stalled_script(generation_app, page):
    base, *_ = generation_app
    pending = []
    page.route('**/static/theme.js?*', lambda route: pending.append(route))
    page.clock.install()
    page.goto(base + '/home/generating', wait_until='commit')
    expect(page.get_by_role('status')).to_contain_text('Wiring up your playlist')
    page.clock.fast_forward(90001)
    expect(page.get_by_role('alert')).to_contain_text('couldn’t confirm playback')
    expect(page.locator('.gen-loader')).to_be_hidden()
    for route in pending:
        route.abort('failed')


@pytest.mark.parametrize('generation_app', ['board'], indirect=True)
@pytest.mark.parametrize('width', [1440, 390])
def test_unknown_mix_keeps_its_identity_and_returns_to_the_same_selection(generation_app, page, width):
    base, provider, *_ = generation_app
    page.set_viewport_size({'width': width, 'height': 900})
    page.unroute('https://i.ytimg.com/**')
    page.context.route('https://i.ytimg.com/**', lambda route: route.fulfill(
        content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300">'
        '<path fill="#503b69" d="M0 0h300v300H0z"/><circle cx="150" cy="150" r="90" fill="#a9cbbd"/></svg>'))
    page.reload()
    card = page.locator('#gen-wheelhouse')
    title = card.locator('.gen-theme').inner_text()
    song = card.locator('.gen-row').first.get_attribute('data-title')
    selected = card.locator('.gen-row').count()
    posts = []

    def lose_receipt(route):
        posts.append(route.request)
        assert route.fetch().status == 200  # The provider save actually finishes.
        route.abort('failed')

    page.route('**/home/generate', lose_receipt)
    card.locator('.gen-poster').click()
    with page.expect_popup() as opened:
        card.get_by_role('button', name='Save and play this mix').click()
    popup = opened.value
    popup.set_viewport_size({'width': width, 'height': 900})
    expect(popup.get_by_role('heading')).to_have_text(title)
    expect(popup.get_by_role('alert')).to_contain_text(song)
    expect(popup.get_by_role('link', name='Find this mix on YouTube Music')).to_have_attribute(
        'href', re.compile('https://music.youtube.com/search\\?q='))
    try:
        popup.wait_for_function("""() => [...document.querySelectorAll('.mix-launch-art img')].every(
            image => image.complete && image.naturalWidth > 0 && image.getBoundingClientRect().height > 0)""",
            timeout=5000)
    except Exception:
        pytest.fail(str(popup.locator('.mix-launch-art img').evaluate_all("""images => images.map(
            image => ({src:image.src, complete:image.complete, width:image.naturalWidth,
                       height:image.getBoundingClientRect().height, visibility:getComputedStyle(image).visibility}))""")))
    popup.screenshot(path=f'/tmp/tuneconsole-634-context-{width}.png', full_page=True)
    popup.get_by_role('button', name='Back to this mix').click()
    deadline = time.monotonic() + 5
    while not popup.is_closed() and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert popup.is_closed()
    expect(card).to_have_class(re.compile('is-focused'))
    assert card.locator('.gen-row').count() == selected
    expect(card.get_by_role('button', name='Save and play this mix')).to_be_disabled()
    assert len(posts) == len(provider.created) == 1


@pytest.mark.parametrize('generation_app', ['starter', 'board'], indirect=True)
def test_popup_deadline_survives_reloading_its_opener(generation_app, page, request):
    base, provider, *_ = generation_app
    page.unroute('https://i.ytimg.com/**')
    page.context.route('https://i.ytimg.com/**', lambda route: route.fulfill(
        content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300">'
        '<path fill="#503b69" d="M0 0h300v300H0z"/><circle cx="150" cy="150" r="90" fill="#a9cbbd"/></svg>'))
    receipts = []
    kind = request.node.callspec.params['generation_app']
    card = page.locator('#gen-wheelhouse' if kind == 'board' else '#gen-onboard_library')
    if kind == 'board':
        card.locator('.gen-poster').click()
        before = card.locator('.gen-row').count()
        card.get_by_role('button', name='Remove from this playlist').first.click()
        expect(card.locator('.gen-row')).to_have_count(before - 1)
    selected = card.evaluate('node => generationContext(node)')

    def hold_receipt(route):
        response = route.fetch()
        assert response.status == 200
        receipts.append(route)

    page.route('**/home/generate', hold_receipt)
    page.clock.install()
    with page.expect_popup() as opened:
        card.get_by_role('button', name='Save and play this mix').click()
    popup = opened.value
    expect(popup.get_by_role('heading')).to_have_text(selected['title'])
    page.reload()
    popup.clock.fast_forward(90001)
    expect(popup.get_by_role('alert')).to_contain_text('couldn’t confirm playback')
    expect(popup.get_by_role('heading')).to_have_text(selected['title'])
    assert len(provider.created) == len(receipts) == 1
    page.clock.resume()
    popup.get_by_role('button', name='Back to this mix').click()
    deadline = time.monotonic() + 5
    while not popup.is_closed() and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert popup.is_closed(), popup.evaluate('({hasOpener:!!opener, url:location.href})')
    expect(card).to_be_visible()
    actual = card.evaluate('node => generationContext(node)')
    assert sorted(actual['tracks'], key=lambda row: row['video_id']) == sorted(
        selected['tracks'], key=lambda row: row['video_id'])
    expect(card.get_by_role('button', name='Save and play this mix')).to_be_disabled()
    expect(card.locator('[data-generation-error]')).to_be_visible()
    page.wait_for_function("""id => {
        const node=document.getElementById(id);
        return getComputedStyle(node).opacity==='1'
            && [...node.querySelectorAll('.gen-row img')].every(image => image.complete
                && image.naturalWidth>0 && image.getBoundingClientRect().height>0);
    }""", arg=selected['id'])
    if kind == 'board':
        expect(page.locator('.mix-opening-mark')).to_be_hidden()
    page.screenshot(path=f'/tmp/tuneconsole-634-restored-selection-{kind}.png', full_page=True)
    # The retained choices must survive a second reload after the popup closes.
    page.clock.resume()
    page.reload()
    expect(card.locator('.gen-row')).to_have_count(len(selected['tracks']))
    assert card.evaluate('node => generationContext(node).tracks') == actual['tracks']
    assert len(provider.created) == 1


@pytest.mark.parametrize('generation_app', ['board'], indirect=True)
@pytest.mark.parametrize('opener_action', ['close', 'navigate'])
def test_edited_mix_recovery_survives_losing_its_opener(generation_app, page, opener_action):
    base, provider, *_ = generation_app
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.unroute('https://i.ytimg.com/**')
    page.context.route('https://i.ytimg.com/**', lambda route: route.fulfill(
        content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300">'
        '<path fill="#503b69" d="M0 0h300v300H0z"/><circle cx="150" cy="150" r="90" fill="#a9cbbd"/></svg>'))
    card = page.locator('#gen-wheelhouse')
    card.locator('.gen-poster').click()
    before = card.locator('.gen-row').count()
    removed = card.locator('.gen-row').first.get_attribute('data-vid')
    card.get_by_role('button', name='Remove from this playlist').first.click()
    expect(card.locator('.gen-row')).to_have_count(before - 1)
    selected = card.evaluate('node => generationContext(node)')
    assert removed not in {track['video_id'] for track in selected['tracks']}
    posts, errors = [], []

    def lose_receipt(route):
        posts.append(route.request)
        assert route.fetch().status == 200
        route.abort('failed')

    page.route('**/home/generate', lose_receipt)
    with page.expect_popup() as opened:
        card.get_by_role('button', name='Save and play this mix').click()
    popup = opened.value
    popup.on('pageerror', lambda error: errors.append(str(error)))
    expect(popup.get_by_role('alert')).to_contain_text('couldn’t confirm playback')
    back = popup.get_by_role('button', name='Back to this mix')
    # Exercise the actual handler after its creating document is destroyed.
    assert back.evaluate('button => button.onclick instanceof window.Function')
    assert len(posts) == len(provider.created) == 1
    if opener_action == 'close':
        page.close()
        returned = popup
    else:
        page.goto(base + '/playlists')
        returned = page
    back.click()
    returned.wait_for_url(base + '/#gen-wheelhouse')
    restored = returned.locator('#gen-wheelhouse')
    expect(restored.locator('.gen-row')).to_have_count(len(selected['tracks']))
    actual = restored.evaluate('node => generationContext(node).tracks')
    assert sorted(actual, key=lambda row: row['video_id']) == sorted(
        selected['tracks'], key=lambda row: row['video_id'])
    play = restored.get_by_role('button', name='Save and play this mix')
    expect(play).to_be_disabled()
    expect(restored.get_by_role('button', name='Regenerate this mix', exact=True)).to_be_disabled()
    expect(restored.locator('[data-generation-error]')).to_be_visible()
    assert restored.locator('.gen-title').first.get_attribute('href').startswith('https://music.youtube.com/watch?v=')
    # Even a synthetic click cannot silently submit another create after the
    # popup becomes the surviving app tab with its own session storage.
    returned.route('**/home/generate', lambda route: (posts.append(route.request), route.abort('failed')))
    play.evaluate("button => htmx.trigger(button, 'click')")
    returned.reload()
    expect(restored.locator('.gen-row')).to_have_count(len(selected['tracks']))
    assert restored.evaluate('node => generationContext(node).tracks') == actual
    expect(restored.get_by_role('button', name='Save and play this mix')).to_be_disabled()
    returned.wait_for_function("""() => [...document.querySelectorAll('#gen-wheelhouse .gen-row img')].every(
        image => image.complete && image.naturalWidth > 0)""")
    returned.screenshot(path=f'/tmp/tuneconsole-634-opener-{opener_action}.png', full_page=True)
    assert len(posts) == len(provider.created) == 1
    assert not errors
