"""Real save requests and popup lifecycle, with a controlled provider/player."""
import base64
import json
from pathlib import Path
import re
import time
from urllib.parse import quote

import pytest
from playwright.sync_api import expect

from tests.conftest import FakeClient, _track
from yt_playlist.library.sync import sync_identity


def _generation_provider():
    return FakeClient(playlists=[{'playlistId': 'SOURCE', 'title': 'Ready', 'count': 2}],
                      tracks={'SOURCE': [_track('one', 'First song', 'Artist'),
                                         _track('two', 'Second song', 'Artist')]})


@pytest.fixture(scope='session')
def generated_board_template(_pristine_current_schema):
    from tests.prepared_fixture_support import prepared_template
    from tests.test_home_other_genres import seed_genres
    from tests.test_mode_cards_route import prepare_home

    def build(source):
        account = source.accounts.upsert_account('main', 'cred', None, True)
        source.playlists.upsert_playlist(account, 'PLZ', 'Old Mix', 3, 'h', 0.0)
        sync_identity(source, account, _generation_provider(), now=1)
        source.settings.set_setting('onboard_listening_format', 'mixes')
        # Keep the original six genres, four lanes and twenty songs per lane.
        seed_genres(source)
        for key, value in (('last_sync_at', '1'), ('onboard_dismissed', '1'),
                           ('home_format', 'mixes')):
            source.settings.set_setting(key, value)
        prepare_home(source, 1)
        catalog = {'v': _track('v', 'Prepared song', 'Artist')}
        for track in source.conn.execute(
                'SELECT video_id,title,artist,duration_s FROM catalog_tracks WHERE video_id IS NOT NULL'):
            catalog[track['video_id']] = _track(track['video_id'], track['title'], track['artist'],
                track['duration_s'] or 200, set_video_id='set-' + track['video_id'])
        return catalog

    with prepared_template(_pristine_current_schema, build) as template:
        yield template


@pytest.fixture
def generated_board_app(store, generated_board_template):
    import socket
    import uvicorn
    from tests.live_server import LiveServer
    from yt_playlist.web.app import create_app

    # Restore before starting the private app; no running request observes a
    # partially copied Store. Providers, bridges and launch state are never shared.
    catalog = generated_board_template.restore(store)
    provider = _generation_provider()
    provider._catalog.update(catalog)
    account = store.accounts.get_master_account().id
    app = create_app(store, lambda: {account: provider}, now_fn=lambda: 1.0)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
        with LiveServer(server, sockets=[sock]):
            yield f'http://127.0.0.1:{port}', app.state.ctx, provider


@pytest.fixture
def generation_app(request, monkeypatch, store, page):
    from yt_playlist.web import app as app_module

    if getattr(request, 'param', None) == 'board':
        base, ctx, provider = request.getfixturevalue('generated_board_app')
        from yt_playlist.rec import account_growth
        growth_view = account_growth.view

        def unlocked_mixes(*args, **kwargs):
            result = growth_view(*args, **kwargs)
            result['ready_formats']['mixes'] = True
            result['ready_areas']['directions'] = True
            return result

        monkeypatch.setattr(account_growth, 'view', unlocked_mixes)
    else:
        original, apps = app_module.create_app, []

        def capture(*args, **kwargs):
            app = original(*args, **kwargs)
            apps.append(app)
            return app

        monkeypatch.setattr(app_module, 'create_app', capture)
        base = request.getfixturevalue('live_app')
        ctx = apps[0].state.ctx
        account = store.accounts.get_master_account().id
        provider = _generation_provider()
        sync_identity(store, account, provider, now=1)
        ctx.client_provider = lambda: {account: provider}
        store.settings.set_setting('onboard_listening_format', 'mixes')
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
    page.route('https://i.ytimg.com/**', lambda route: route.abort())
    page.goto(base)
    return base, provider, bridge, frames


@pytest.fixture
def save_deadline(generation_app, page, stalled_routes, request):
    # HTMX sends XMLHttpRequest, whose timeout is native and is not advanced
    # by Playwright's JavaScript clock. Keep the production deadline installed
    # until the real save has finished, then shorten that same native timer.
    page.evaluate('''() => {
      document.addEventListener('htmx:beforeSend', event => {
        if (event.detail.requestConfig.path !== '/home/generate') return;
        const xhr = event.detail.xhr;
        const state = window.generatedSaveDeadline = {
          requested: event.detail.requestConfig.timeout, installed: xhr.timeout,
          started: performance.now(), armed: false, timedOut: false
        };
        xhr.addEventListener('timeout', event => {
          state.timedOut = true; state.trusted = event.isTrusted;
          state.expiredAt = performance.now();
        });
        state.arm = () => {
          state.armedAt = performance.now();
          state.shortened = Math.ceil(state.armedAt - state.started) + 200;
          xhr.timeout = state.shortened;
          state.armed = true;
        };
      });
    }''')

    def expire(popup):
        _, provider, _, frames = generation_app
        deadline = time.monotonic() + 10
        while not stalled_routes and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        assert len(stalled_routes) == len(provider.created) == len(frames) == 1
        state = page.evaluate('''() => ({requested: generatedSaveDeadline.requested,
          installed: generatedSaveDeadline.installed, armed: generatedSaveDeadline.armed,
          timedOut: generatedSaveDeadline.timedOut})''')
        assert state == {'requested': 20000, 'installed': 20000, 'armed': False, 'timedOut': False}
        assert page.evaluate('() => [...generationWindows.values()].every(record => !record.uncertain)')
        assert not popup.is_closed()
        expect(popup.get_by_role('status', name='Preparing playlist')).to_be_visible()
        expect(popup.get_by_role('alert')).not_to_be_visible()
        page.evaluate('generatedSaveDeadline.arm()')
        expect(popup.get_by_role('alert')).to_contain_text('couldn’t confirm playback')
        state = page.evaluate('''() => ({...generatedSaveDeadline, arm: undefined})''')
        assert state['armed'] and state['timedOut'] and state['trusted']
        assert state['shortened'] < 20000
        assert state['expiredAt'] >= state['armedAt']
        assert state['expiredAt'] - state['started'] >= state['shortened'] - 1
        report = '/tmp/tuneconsole-1001-save-deadline-' + re.sub(r'[^\w.-]', '-', request.node.name) + '.json'
        Path(report).write_text(json.dumps({'test': request.node.nodeid, 'native_deadline': state,
            'completed_saves': len(provider.created), 'playback_frames': len(frames)}, indent=2))
        return state

    return expire




def test_save_keeps_loader_until_playlist_opens_without_a_confirmation_screen(generation_app, page):
    base, provider, bridge, frames = generation_app
    pending, errors = {}, []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def hold_save(route):
        pending['response'] = route.fetch()
        pending['route'] = route

    page.route('**/home/generate', hold_save)
    page.evaluate('''() => {
      window.confirmationScreens = 0;
      new MutationObserver(records => {
        for (const record of records) for (const node of record.addedNodes) {
          if (node.nodeType === 1 && (node.matches('.gen-done') || node.querySelector('.gen-done')))
            window.confirmationScreens++;
        }
      }).observe(document.body, {childList: true, subtree: true});
      window.addEventListener('app-page-leaving', () => {
        const record = Array.from(generationWindows.values())[0];
        sessionStorage.setItem('mix-navigation-check', JSON.stringify({
          confirmations: window.confirmationScreens,
          loaderOpen: !!record?.tab && !record.tab.closed
        }));
      }, {once: true});
      window.addEventListener('app-page-changed', () => {
        const record = Array.from(generationWindows.values())[0];
        sessionStorage.setItem('mix-ready-check', JSON.stringify({
          confirmations: window.confirmationScreens,
          loaderOpen: !!record?.tab && !record.tab.closed,
          destinationReady: !!document.querySelector('.pl-headmain')
        }));
      }, {once: true});
    }''')
    with page.expect_popup() as opened:
        page.get_by_role('button', name='Save and play this mix').click()
    popup = opened.value
    try:
        deadline = time.monotonic() + 10
        while 'route' not in pending and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        assert 'route' in pending, 'The save did not finish'
        assert page.evaluate('window.confirmationScreens') == 0
        expect(page.locator('.gen-done')).to_have_count(0)
        expect(page.locator('.starter-mix')).to_be_visible()
        assert not popup.is_closed()
        expect(popup.locator('.gen-stage')).to_be_visible()
        popup.screenshot(path='/tmp/tuneconsole-mix-loader.png')
    finally:
        page.unroute('**/home/generate', hold_save)
        if 'route' in pending:
            pending.pop('route').fulfill(response=pending['response'])
    page.wait_for_url(re.compile(r'/playlist/\d+$'))
    expect(page.locator('.pl-headmain')).to_be_visible()
    if not popup.is_closed():
        popup.wait_for_event('close', timeout=10000)
    assert popup.is_closed()
    assert page.evaluate("JSON.parse(sessionStorage.getItem('mix-navigation-check'))") == {
        'confirmations': 0, 'loaderOpen': True}
    assert page.evaluate("JSON.parse(sessionStorage.getItem('mix-ready-check'))") == {
        'confirmations': 0, 'loaderOpen': True, 'destinationReady': True}
    page.evaluate('window.playbackStatus.refresh()')
    expect(page.locator('[data-generated-playback]')).to_have_count(0)
    page.screenshot(path='/tmp/tuneconsole-mix-normal-playlist.png')
    assert len(provider.created) == len(frames) == 1
    assert provider.created[0][0] in frames[0]['url']
    assert not errors














@pytest.mark.parametrize('generation_app', ['board'], indirect=True)
@pytest.mark.parametrize('failure', ['create'])
def test_unconfirmed_provider_create_cannot_be_repeated_after_reload(generation_app, page, failure):
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

    if failure == 'create':
        provider.create_playlist = applied_without_reply
    else:
        add, read = provider.add_playlist_items, provider.get_playlist

        def addition_without_reply(*args, **kwargs):
            add(*args, **kwargs)
            return {'status': 'UNKNOWN'}

        def stale_read(playlist, **kwargs):
            detail = read(playlist, **kwargs)
            return {**detail, 'tracks': detail['tracks'][:1]} if playlist.startswith('PL_NEW_') else detail

        provider.add_playlist_items = addition_without_reply
        provider.get_playlist = stale_read
    with page.expect_popup() as opened:
        card.get_by_role('button', name='Save and play this mix').click()
    expect(page.locator('.gen-done')).to_contain_text('Partially saved' if failure == 'addition' else 'Playback unconfirmed')
    if not opened.value.is_closed():
        opened.value.wait_for_event('close', timeout=5000)
    assert opened.value.is_closed()
    expect(page.locator('.gen-done')).to_contain_text(
        'Some songs are still unconfirmed.' if failure == 'addition' else 'We couldn’t confirm playback.')
    songs = page.get_by_role('list', name='Your selected songs')
    for index, track in enumerate(selected):
        row = songs.get_by_role('listitem').nth(index)
        expect(row).to_contain_text(track['title'])
        expect(row).to_contain_text(track['artist'])
        expect(row.get_by_role('link')).to_have_attribute('href', 'https://music.youtube.com/watch?v=' + quote(track['video']))
    assert songs.get_by_role('listitem').count() == len(selected)
    page.wait_for_function("""() => [...document.querySelectorAll('.gen-recovery-tracks img')].every(
        image => image.complete && image.naturalWidth > 0)""")
    page.screenshot(path=f'/tmp/tuneconsole-634-provider-{failure}-uncertain.png', full_page=True)
    if failure == 'addition':
        expect(page.get_by_role('link', name='Open saved mix')).to_have_attribute(
            'href', 'https://music.youtube.com/playlist?list=' + provider.created[0][0])
        expect(page.locator('[data-generation-error]')).to_contain_text(f'1 saved · {len(selected) - 1} unconfirmed')
        page.get_by_role('button', name='Check saved mix').click()
        expect(page.get_by_role('button', name='Check saved mix')).to_be_enabled()
        expect(page.locator('[data-generation-error]')).to_contain_text('unconfirmed')
    else:
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
    expect(card.locator('.gen-refresh')).to_be_disabled()
    card.get_by_role('button', name='Save and play this mix').evaluate("button => htmx.trigger(button, 'click')")
    if failure == 'addition':
        assert [t['videoId'] for t in provider._tracks[provider.created[0][0]]] == [t['video'] for t in selected]
        expect(card.locator('[data-generation-error]')).to_contain_text('unconfirmed')
        provider.get_playlist = read
        card.get_by_role('button', name='Check saved mix').click()
        expect(card.locator('[data-generation-error]')).to_have_text(re.compile(f'^{len(selected)} saved\\.'))
        expect(card.locator('[data-generation-progress]')).to_have_count(len(selected))
        page.reload()
        expect(card.locator('[data-generation-error]')).to_have_text(re.compile(f'^{len(selected)} saved\\.'))
        expect(card.get_by_role('button', name='Save and play this mix')).to_be_disabled()
        page.screenshot(path='/tmp/tuneconsole-634-addition-restored.png', full_page=True)
        assert len(provider.added) == 1 and not frames
    assert len(provider.created) == 1
