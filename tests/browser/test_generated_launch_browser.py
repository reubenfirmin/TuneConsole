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


def test_prepared_board_copies_keep_catalog_sql_and_files_private(
        generated_board_template, store, _pristine_current_schema):
    from contextlib import closing
    from tests.store_fixture_support import initialize_consumer
    from yt_playlist.core.store import Store
    from yt_playlist.rec import home_prepared, mode_surfaces

    template = generated_board_template
    bundles = template.bundles
    assert {str(mid) for mid in range(6)} <= bundles.keys()
    assert all(set(bundles[str(mid)]) == set(mode_surfaces.CARD_SURFACES) for mid in range(6))
    assert all(len(rows) == 20 for mid in range(6) for rows in bundles[str(mid)].values())
    first_catalog = template.restore(store)
    sibling = Store(':memory:')
    with closing(sibling.conn):
        initialize_consumer(sibling, _pristine_current_schema)
        second_catalog = template.restore(sibling)
        owners = (template.source, store, sibling)
        assert len({id(owner.conn) for owner in owners}) == 3
        assert len({owner._evidence_key for owner in owners}) == 3
        expected = template.source.rec_proposals.get_proposals(home_prepared.SURFACE)
        assert home_prepared._valid(expected)
        for owner in owners:
            assert owner.rec_proposals.get_proposals(home_prepared.SURFACE) == expected
            assert len(owner.modes.list_modes()) == 6
            source = next(p for p in owner.playlists.get_playlists() if p.provider_playlist_id == 'SOURCE')
            assert [track[1] for track in owner.playlists.get_playlist_tracks_with_meta(source.id)] == ['one', 'two']

        def files(owner):
            return {tuple(row[:3]): Path(row[3]) for row in owner.conn.execute(
                'SELECT publication_id,surface,chunk_key,filename FROM home_display_files')}

        original, first, second = (files(owner) for owner in owners)
        assert original and original.keys() == first.keys() == second.keys()
        before = {key: path.read_bytes() for key, path in original.items()}
        for key, path in original.items():
            assert first[key].read_bytes() == second[key].read_bytes() == before[key]
            assert not path.samefile(first[key]) and not path.samefile(second[key])
            assert not first[key].samefile(second[key])
        first_provider, second_provider = _generation_provider(), _generation_provider()
        first_provider._catalog.update(first_catalog)
        second_provider._catalog.update(second_catalog)
        title = second_provider._catalog['v']['title']
        first_provider._catalog['v']['title'] = 'Private edit'
        first_provider._tracks['SOURCE'].append(_track('private', 'Only here', 'Artist'))
        first_provider.create_playlist('Private save', 'Only this test')
        assert second_provider._catalog['v']['title'] == template.result['v']['title'] == title
        assert len(second_provider._tracks['SOURCE']) == 2 and not second_provider.created
        store.settings.set_setting('generated_board_private', 'changed')
        store.rec_proposals.put_proposals(home_prepared.SURFACE, {**expected, 'built_at': 2}, 2)
        for owner in (sibling, template.source):
            assert owner.settings.get_setting('generated_board_private') is None
            assert owner.rec_proposals.get_proposals(home_prepared.SURFACE) == expected
        assert {key: path.read_bytes() for key, path in original.items()} == before
        assert {key: path.read_bytes() for key, path in second.items()} == before


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


def test_uncertain_save_allows_a_different_synced_selection_and_keeps_original_recovery(
        generation_app, store, page, stalled_routes, save_deadline):
    base, provider, bridge, frames = generation_app
    posts, errors, popups = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    original = page.locator('.starter-mix').evaluate('node => generationContext(node)')

    def hold_only_first_receipt(route):
        posts.append(route.request)
        response = route.fetch()
        assert response.status == 200
        if len(posts) > 1:
            route.fulfill(response=response)
        else:
            stalled_routes.append(route)

    page.route('**/home/generate', hold_only_first_receipt)
    try:
        with page.expect_popup() as opened:
            page.get_by_role('button', name='Save and play this mix').click()
        unknown_popup = opened.value
        popups.append(unknown_popup)
        save_deadline(unknown_popup)
        expect(unknown_popup.get_by_role('alert')).to_contain_text('couldn’t confirm playback', timeout=30000)
        expect(page.locator('#toasts [role="alert"]')).to_have_count(0)
        old = page.evaluate('''() => {
          const record = generationWindows.get('gen-onboard_library');
          return {key: record.key, token: record.token};
        }''')
        page.evaluate('genComplete("a-different-launch-token", null, true)')
        assert not unknown_popup.is_closed()
        third = _track('three', 'Third song', 'Another artist')
        provider._tracks['SOURCE'].append(third)
        provider._playlists[0]['count'] = 3
        provider._catalog['three'] = third
        sync_identity(store, store.accounts.get_master_account().id, provider, now=2)
        expect(page.locator('#connection-error')).to_have_count(0)
        card = page.locator('.starter-mix')
        expect(card.locator('.gen-row')).to_have_count(3, timeout=10000)
        assert {track['video_id'] for track in card.evaluate('node => generationContext(node).tracks')} == {
            'one', 'two', 'three'}
        play = card.get_by_role('button', name='Save and play this mix')
        expect(play).to_be_enabled()
        with page.expect_popup() as opened_new:
            play.click()
        new_popup = opened_new.value
        popups.append(new_popup)
        page.wait_for_url(re.compile(r'/playlist/\d+$'))
        page.wait_for_function('() => !window.__ytTab || window.__ytTab.closed')
        if not new_popup.is_closed():
            new_popup.wait_for_event('close', timeout=10000)
        assert new_popup.is_closed()
        assert not unknown_popup.is_closed()
        assert len(posts) == len(provider.created) == len(frames) == 2
        assert {track['videoId'] for track in provider._tracks[provider.created[0][0]]} == {'one', 'two'}
        assert {track['videoId'] for track in provider._tracks[provider.created[1][0]]} == {'one', 'two', 'three'}
        assert page.evaluate('key => sessionStorage.getItem(key)', old['key']) == old['token']
        assert page.evaluate('key => JSON.parse(sessionStorage.getItem(key)).tracks',
                             'tc-mix-launch:' + old['token']) == original['tracks']
        # The first popup still restores its own attempted two-song context
        # after a different three-song save completed and retired its window.
        unknown_popup.get_by_role('button', name='Back to this mix').click()
        page.wait_for_url(base + '/#gen-onboard_library')
        expect(card.locator('.gen-row')).to_have_count(2)
        assert card.evaluate('node => generationContext(node).tracks') == original['tracks']
        expect(play).to_be_disabled()
        play.evaluate("button => htmx.trigger(button, 'click')")
        assert len(posts) == len(provider.created) == len(frames) == 2
        page.screenshot(path='/tmp/tuneconsole-881-original-after-different-save.png', full_page=True)
        assert not errors
    finally:
        Path('/tmp/tuneconsole-881-different-save-diagnostics.json').write_text(json.dumps({
            'posts': [request.post_data for request in posts],
            'created': provider.created, 'frames': frames,
            'windows': page.evaluate('''() => [...generationWindows.values()].map(record => ({
              token: record.token, key: record.key, sent: record.sent, uncertain: record.uncertain,
              connected: record.node.isConnected, tabClosed: record.tab?.closed,
              currentTab: window.__ytTab === record.tab
            }))'''),
            'popups': [{'url': popup.url, 'closed': popup.is_closed()} for popup in popups],
            'errors': errors,
        }, indent=2))
        page.unroute_all(behavior='ignoreErrors')
        for popup in popups:
            if not popup.is_closed():
                popup.close()


@pytest.mark.parametrize('generation_app', ['board'], indirect=True)
@pytest.mark.parametrize('refresh', ['cards', 'sync', 'recovery'])
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
    page.evaluate("""({url, refresh}) => {
      if (refresh === 'recovery') {
        window.dispatchEvent(Object.assign(new Event('app-request-failed'),
          {detail: {url: '/home/generate', method: 'POST', reconcile: true}}));
        return;
      }
      htmx.ajax('GET', url, {
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
    expect(page.locator('.pl-headmain')).to_be_visible()
    if not opened.value.is_closed():
        opened.value.wait_for_event('close', timeout=10000)
    assert opened.value.is_closed()
    page.evaluate('window.playbackStatus.refresh()')
    expect(page.locator('[data-generated-playback]')).to_have_count(0)
    assert len(provider.created) == len(frames) == 1
    assert not errors


def test_lost_save_response_is_terminal_and_does_not_create_again(generation_app, page):
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
    expect(play).to_be_disabled()
    play.evaluate("button => htmx.trigger(button, 'click')")
    assert len(provider.created) == len(requests) == 1
    page.screenshot(path='/tmp/tuneconsole-634-save-unknown.png', full_page=True)
    popup.screenshot(path='/tmp/tuneconsole-634-popup-failure.png')


def test_uncertain_save_survives_quiet_recovery_before_popup_return(
        generation_app, page, stalled_routes, save_deadline):
    base, provider, bridge, frames = generation_app
    posts, errors = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    selected = page.locator('.starter-mix').evaluate('node => generationContext(node)')

    def lose_receipt(route):
        posts.append(route.request)
        assert route.fetch().status == 200
        # Keep the committed response silent until the real browser deadline.
        stalled_routes.append(route)

    page.route('**/home/generate', lose_receipt)
    page.evaluate("""() => {
        window.saveTimeouts = [];
        document.addEventListener('htmx:beforeSend', event => {
            if (event.detail.requestConfig.path === '/home/generate')
                saveTimeouts.push(event.detail.requestConfig.timeout);
        });
    }""")
    try:
        with page.expect_popup() as opened:
            page.get_by_role('button', name='Save and play this mix').click()
        popup = opened.value
        save_deadline(popup)
        expect(popup.get_by_role('alert')).to_contain_text('couldn’t confirm playback', timeout=30000)
        expect(page.locator('#toasts [role="alert"]')).to_have_count(0)
        assert page.evaluate('saveTimeouts') == [20000]
        assert len(posts) == len(provider.created) == len(frames) == 1
        with page.expect_response(lambda response: response.request.method == 'GET' and response.url == base + '/'):
            pass
        expect(page.locator('#connection-error')).to_have_count(0)
        play = page.get_by_role('button', name='Save and play this mix')
        expect(play).to_be_visible()
        # Change preview order without using Back to this mix. The unresolved
        # identity belongs to the selected videos, not their sampled order.
        page.locator('.starter-tracks').evaluate('list => list.replaceChildren(...[...list.children].reverse())')
        if not play.is_disabled():
            play.click()
        expect(play).to_be_disabled()
        expect(page.locator('[data-generation-error]')).to_contain_text('may already be')
        assert len(posts) == len(provider.created) == len(frames) == 1
        play.evaluate("button => htmx.trigger(button, 'click')")
        expect(play).to_be_disabled()
        expect(page.locator('[data-generation-error]')).to_contain_text('may already be')
        assert len(posts) == len(provider.created) == len(frames) == 1
        # The original popup still owns the attempted order and choices.
        popup.get_by_role('button', name='Back to this mix').click()
        restored = page.locator('.starter-mix')
        expect(restored.locator('.gen-row')).to_have_count(len(selected['tracks']))
        assert restored.evaluate('node => generationContext(node).tracks') == selected['tracks']
        expect(restored.get_by_role('button', name='Save and play this mix')).to_be_disabled()
        page.screenshot(path='/tmp/tuneconsole-881-reconnect.png', full_page=True)
        assert len(posts) == len(provider.created) == len(frames) == 1
        assert not errors
    finally:
        page.unroute_all(behavior='ignoreErrors')


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


@pytest.mark.parametrize('connected', [True, False])
def test_paused_storage_can_open_saved_mix_without_a_local_playlist(generation_app, store, page, connected):
    base, provider, bridge, frames = generation_app
    create = provider.create_playlist

    def disconnect_during_create(*args, **kwargs):
        if not connected:
            bridge._send = None
        return create(*args, **kwargs)

    provider.create_playlist = disconnect_during_create
    store.activity_paused = True
    store._evidence_maintenance = 1
    store.conn.execute('PRAGMA query_only=ON')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        with page.expect_popup() as opened:
            page.get_by_role('button', name='Save and play this mix').click()
        page.wait_for_url(re.compile(r'/playlist/saved/\d+/[^/]+$'))
        expect(page.locator('.pl-headmain')).to_be_visible()
        expect(page.locator('#playlist-track-body .srow')).to_have_count(2)
        expect(page.locator('.playlist-play')).to_be_visible()
        expect(page.locator('.gen-done')).to_have_count(0)
        expect(page.locator('#storage-maintenance')).to_have_text(
            'Optimizing app storage; some recommendations are temporarily unavailable.')
        deadline = time.monotonic() + 5
        while not opened.value.is_closed() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert opened.value.is_closed()
        page.evaluate('window.playbackStatus.refresh()')
        if not connected:
            note = page.locator('[data-generated-playback]')
            expect(note).to_contain_text('Reconnect the YouTube Music extension')
            expect(note).to_contain_text('Use Play')
            expect(note.get_by_role('link')).to_have_attribute('href', re.compile('watch\\?list='))
        else:
            page.wait_for_function('!sessionStorage.getItem("tc-generated-playback")')
            expect(page.locator('[data-generated-playback]')).to_have_count(0)
        page.reload()
        expect(page.locator('#playlist-track-body .srow')).to_have_count(2)
        page.screenshot(path=f'/tmp/tuneconsole-686-saved-playlist-{connected}.png', full_page=True)
        assert len(provider.created) == 1 and len(frames) == int(connected)
        assert len(store.playlists.get_playlists()) == 1
        assert not errors
    finally:
        store.conn.execute('PRAGMA query_only=OFF')
        store.activity_paused = False
        store._evidence_maintenance = 0


def test_playback_timeout_retries_the_saved_playlist(generation_app, page, monkeypatch):
    base, provider, bridge, frames = generation_app
    navigate = bridge.send_control
    # The first command is accepted but never reaches the native player.
    monkeypatch.setattr(bridge, 'send_control', lambda *args, **kwargs: True)
    with page.expect_popup() as opened:
        page.get_by_role('button', name='Save and play this mix').click()
    page.wait_for_url(re.compile(r'/playlist/\d+$'))
    expect(page.locator('.pl-headmain')).to_be_visible()
    if not opened.value.is_closed():
        opened.value.wait_for_event('close', timeout=10000)
    assert opened.value.is_closed()
    page.wait_for_function('sessionStorage.getItem("tc-generated-playback") !== null')
    page.clock.install()
    page.evaluate('window.playbackStatus.refresh()')
    note = page.locator('[data-generated-playback]')
    expect(note).to_have_count(0)
    page.clock.fast_forward(46000)
    expect(note).to_contain_text('Playback has not started')
    monkeypatch.setattr(bridge, 'send_control', navigate)
    page.get_by_role('button', name='Play on YouTube Music', exact=True).click()
    page.evaluate('window.playbackStatus.refresh()')
    expect(note).to_have_count(0)
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


def test_popup_has_feedback_before_the_backend_answers(generation_app, page, stalled_routes):
    base, provider, *_ = generation_app
    requests = stalled_routes
    page.context.route('**/home/generating?*', lambda route: requests.append(route))
    page.route('**/home/generate', lambda route: requests.append(route))
    page.clock.install()
    with page.expect_popup() as opened:
        page.get_by_role('button', name='Save and play this mix').click()
    popup = opened.value
    # The committed local document uses the existing spoke loader while the
    # generation POST is held, without waiting for a backend page response.
    expect(popup.get_by_role('status', name='Preparing playlist')).to_be_visible()
    expect(popup.locator('.gen-hub')).to_be_visible()
    expect(popup.locator('.gen-node')).to_have_count(2)
    first = popup.locator('.gen-node').first
    position = first.evaluate('node => node.style.transform')
    popup.wait_for_function('position => document.querySelector(".gen-node").style.transform !== position', arg=position)
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


@pytest.mark.parametrize('asset', ['generating', 'theme', 'stylesheets'])
def test_interstitial_timeout_survives_unavailable_animation(generation_app, page, asset):
    base, *_ = generation_app
    if asset == 'stylesheets':
        page.route('**/static/*.css?*', lambda route: route.abort('failed'))
    else:
        page.route(f'**/static/{asset}.js?*', lambda route: route.abort('failed'))
    page.clock.install()
    page.goto(base + '/home/generating')
    page.clock.fast_forward(90001)
    expect(page.get_by_role('alert')).to_contain_text('couldn’t confirm playback')
    expect(page.locator('.gen-loader')).to_be_hidden()
    recovery = page.get_by_role('alert')
    # Recovery must remain legible even when the token stylesheet never loads.
    colors = recovery.evaluate('node => ({background:getComputedStyle(node).backgroundColor, color:getComputedStyle(node).color})')
    assert colors['background'] != 'rgba(0, 0, 0, 0)' and colors['background'] != colors['color']
    if asset == 'stylesheets':
        page.screenshot(path='/tmp/tuneconsole-791-recovery-no-stylesheets.png', full_page=True)


def test_interstitial_timeout_survives_a_stalled_script(generation_app, page):
    base, *_ = generation_app
    pending = []
    page.route('**/static/theme.js?*', lambda route: pending.append(route))
    page.clock.install()
    page.add_init_script('''(() => {
      window.mixLaunchDeadlines = [];
      const schedule = window.setTimeout;
      window.setTimeout = function(callback, delay, ...args) {
        const timer = schedule.call(this, callback, delay, ...args);
        if (delay === 90000) window.mixLaunchDeadlines.push(timer);
        return timer;
      };
    })();''')
    try:
        page.goto(base + '/home/generating', wait_until='commit')
        expect(page.get_by_role('status')).to_contain_text('Wiring up your playlist')
        # The loader markup precedes the recovery script in the HTML stream.
        # Advance from its actual deadline installation while theme.js stays held.
        page.wait_for_function('window.mixLaunchDeadlines?.length === 1')
        assert pending
        page.clock.fast_forward(90001)
        expect(page.get_by_role('alert')).to_contain_text('couldn’t confirm playback')
        expect(page.locator('.gen-loader')).to_be_hidden()
        # CSS animation timing is independent of the simulated deadline clock.
        expect(page.get_by_role('alert')).to_have_css('opacity', '1')
        page.screenshot(path='/tmp/tuneconsole-791-stalled-script-recovery.png', full_page=True)
    finally:
        for route in pending:
            route.abort('failed')


@pytest.mark.parametrize('generation_app', ['board'], indirect=True)
@pytest.mark.parametrize('width', [1440])
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
def test_popup_deadline_survives_reloading_its_opener(generation_app, page, request, stalled_routes):
    base, provider, *_ = generation_app
    page.unroute('https://i.ytimg.com/**')
    page.context.route('https://i.ytimg.com/**', lambda route: route.fulfill(
        content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300">'
        '<path fill="#503b69" d="M0 0h300v300H0z"/><circle cx="150" cy="150" r="90" fill="#a9cbbd"/></svg>'))
    receipts = stalled_routes
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
    expect(popup.get_by_role('status', name='Preparing playlist')).to_be_visible()
    expect(popup.locator('.gen-node')).to_have_count(min(14, len(selected['tracks'])))
    popup.wait_for_function('''() => [...document.querySelectorAll('.gen-node img')].every(
        image => image.complete && image.naturalWidth > 0)''')
    popup.screenshot(path=f'/tmp/tuneconsole-686-spokes-{kind}.png')
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
    expect(restored).to_have_class(re.compile('is-focused'))
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
