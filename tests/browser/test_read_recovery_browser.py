"""Expired SQLite reads keep a usable document and retry without user action."""
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.browser_fixtures import _free_port
from tests.live_server import LiveServer
from yt_playlist.core.activity import WorkBudgetExceeded, work_budget
from yt_playlist.web.app import create_app


@pytest.fixture
def recovery_app(file_backed_store, monkeypatch):
    store = file_backed_store
    account = store.accounts.upsert_account('main', 'cred', None, True)
    entry = store.tracks.upsert_track('v', 'Song', 'Artist', None, 100)
    playlist = store.playlists.upsert_playlist(account, 'PL', 'My music', 1, 'h', 1)
    store.playlists.set_playlist_tracks(playlist, [entry])
    store.settings.set_setting('last_sync_at', '1')
    store.settings.set_setting('onboard_dismissed', '1')
    state = SimpleNamespace(remaining=2, attempts=[], connections=[], mutations=0, confirmed_mutations=0)
    original = store.collection.collection_artists

    def artists():
        state.attempts.append(time.monotonic())
        if state.remaining:
            state.remaining -= 1
            with store.read_view() as reader:
                state.connections.append(reader.conn)
                with work_budget(.005):
                    reader.conn.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                        'SELECT x+1 FROM n WHERE x<100000000) SELECT SUM(x) FROM n').fetchone()
        return original()

    monkeypatch.setattr(store.collection, 'collection_artists', artists)
    # Recovery pages must keep the same heartbeat that prevents a packaged
    # backend from deciding its UI has closed after a plain error document.
    monkeypatch.setenv('YT_PLAYLIST_EXIT_ON_IDLE', '1')
    app = create_app(store, lambda: {}, now_fn=lambda: 1, background_work=False)

    @app.get('/test-fragment')
    def fragment(q: str):
        artists()
        from fastapi.responses import HTMLResponse
        return HTMLResponse(f'<p>Loaded {q}</p>')

    @app.post('/test-mutation')
    def mutation():
        state.mutations += 1
        raise WorkBudgetExceeded('Mutation expired')

    @app.get('/test-json-read')
    def json_read():
        artists()
        return {'loaded': True}

    @app.post('/test-confirmed-mutation')
    def confirmed_mutation():
        state.confirmed_mutations += 1
        return {'saved': state.confirmed_mutations}

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    with LiveServer(server):
        yield f'http://127.0.0.1:{port}', state, store


def test_initial_document_recovers_from_repeated_sql_timeouts(page, recovery_app, monkeypatch):
    from tests.browser.test_audio_vibes_browser import CARDS, DESCRIPTIONS, seed_samples, visible_card

    monkeypatch.setenv('YT_PLAYLIST_AUDIO_VIBES', '1')
    url, state, store = recovery_app
    value, = seed_samples(store, count=1)
    record = {'id': value['run_id'], 'dismissed': False, 'draft': None, 'dirty': False,
              'sample_ids': [], 'playback': {key: value[key] for key in ('video_id', 'title', 'artist')}}
    page.add_init_script('localStorage.setItem(' + json.dumps(CARDS) + ', '
                         + json.dumps(json.dumps([record])) + ');')
    delivery = {'offline': True}
    page.route('**/audio-vibes/feedback', lambda route: route.fulfill(
        status=503, json={'detail': 'Controlled label delivery outage'})
        if delivery['offline'] else route.continue_())
    errors, heartbeats = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: heartbeats.append(request.url)
            if '/heartbeat?' in request.url else None)
    page.set_viewport_size({'width': 1440, 'height': 900})
    assert page.goto(url + '/artists').status == 503
    page.evaluate('window.recoveryDocument=document;window.vibeRoot=document.querySelector("[data-audio-vibe-banner]")')
    expect(page.locator('[data-audio-vibe-collection]')).to_have_count(1)
    expect(page.locator('[data-audio-vibe-banner]')).to_have_count(1)
    expect(page.get_by_role('heading', name='Loading your library…')).to_be_visible()
    expect(page.locator('.topbar').get_by_role('link', name='Playlists', exact=True)).to_be_visible()
    page.screenshot(path='/tmp/tuneconsole-882-read-pending.png')
    expect(page.locator('main h1')).to_have_text('Artists', timeout=15000)
    expect(page.locator('main').get_by_role('link', name='Artist', exact=True)).to_be_visible()
    assert page.evaluate('document===window.recoveryDocument && window.vibeRoot===document.querySelector("[data-audio-vibe-banner]")')
    card = visible_card(page)
    expect(card.locator('[data-vibe-track]')).to_have_text(value['title'])
    card.get_by_role('button', name=DESCRIPTIONS['concentrate'], exact=True).click()
    page.wait_for_function('JSON.parse(localStorage.getItem(' + json.dumps(CARDS) + '))[0].dirty')
    page.evaluate('window.retainedVibeCard=document.querySelector("[data-vibe-card]")')
    # Enter and leave preview through the app's intercepted links after recovery.
    page.locator('.topbar').get_by_role('button', name='Tools', exact=True).click()
    page.locator('.topbar').get_by_role('link', name='Audio analysis preview', exact=True).click()
    expect(page.locator('[data-audio-preview]')).to_be_visible()
    expect(page.locator('[data-audio-vibe-banner]')).to_be_hidden()
    page.locator('.topbar').get_by_role('link', name='Playlists', exact=True).click()
    expect(page.locator('main h1')).to_have_text('Playlists')
    assert page.evaluate('document===window.recoveryDocument && window.vibeRoot===document.querySelector("[data-audio-vibe-banner]")')
    assert page.evaluate('window.retainedVibeCard===document.querySelector("[data-vibe-card]")')
    expect(card.get_by_role('button', name=DESCRIPTIONS['concentrate'], exact=True)).to_have_attribute('aria-pressed', 'true')
    delivery['offline'] = False
    expect(card.locator('[data-vibe-feedback-status]')).to_have_text('Saved', timeout=15000)
    answer = store.audio_vibes.feedback(value['run_id'])
    assert answer['vibes'] == ['concentrate'] and answer['sample_ids'] == [value['id']]
    expect(page.locator('#navigation-error')).to_have_count(0)
    assert len(state.attempts) == 3
    assert state.attempts[2] - state.attempts[1] >= 1.8
    assert heartbeats and not errors
    for connection in state.connections:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connection.execute('SELECT 1')
    # The timed-out requests released every admission slot and the writer.
    permits = []
    try:
        for _ in range(4):
            assert store._analytics_readers.acquire(blocking=False)
            permits.append(True)
    finally:
        for _ in permits:
            store._analytics_readers.release()
    store.settings.set_setting('after_timeout', 'saved')
    assert store.settings.get_setting('after_timeout') == 'saved'
    page.screenshot(path='/tmp/tuneconsole-882-read-recovered.png')


def test_successful_takeout_import_retargets_the_completed_form(page, recovery_app):
    from tests.web.test_takeout_route import _entry

    url, state, store = recovery_app
    state.remaining = 0
    page.goto(url + '/setup?tab=import')
    form = page.locator('form[hx-post="/import/takeout"]')
    form.locator('input[type="file"]').set_input_files({
        'name': 'watch-history.txt', 'mimeType': 'text/plain', 'buffer': b'unreadable history'})
    form.get_by_role('button', name='Import history', exact=True).click()
    expect(page.locator('#takeout-import-result')).to_contain_text('JSON')
    expect(form).to_have_count(1)
    assert store.settings.get_setting('takeout_imported_at') is None
    payload = json.dumps([_entry('Song', 'Artist', 'v', '2024-01-01T10:00:00Z')]).encode()
    form.locator('input[type="file"]').set_input_files({
        'name': 'watch-history.json', 'mimeType': 'application/json', 'buffer': payload})
    with page.expect_response('**/import/takeout') as response:
        form.get_by_role('button', name='Import history', exact=True).click()
    assert response.value.status == 200
    assert response.value.headers['hx-retarget'] == '#takeout-import-block'
    assert store.settings.get_setting('takeout_imported_at') is not None
    expect(page.locator('#takeout-import-block')).to_contain_text('Import complete')
    expect(form).to_have_count(0)
    page.screenshot(path='/tmp/tuneconsole-865-takeout-complete.png', full_page=True)


def test_leaving_pending_document_cancels_its_retries(page, recovery_app):
    url, state, _ = recovery_app
    state.remaining = 100
    page.goto(url + '/artists')
    expect(page.get_by_role('heading', name='Loading your library…')).to_be_visible()
    page.locator('.topbar').get_by_role('link', name='Playlists', exact=True).click()
    expect(page.locator('main h1')).to_have_text('Playlists')
    attempts = len(state.attempts)
    page.wait_for_timeout(2500)
    assert len(state.attempts) == attempts
    expect(page.locator('main h1')).to_have_text('Playlists')


def test_htmx_keeps_content_and_query_parameters_until_read_recovers(page, recovery_app):
    url, state, _ = recovery_app
    state.remaining = 1
    page.goto(url + '/playlists')
    page.evaluate('''() => {
        const region = document.createElement('section');
        region.innerHTML = `<button hx-get="/test-fragment" hx-target="#recover-fragment"
            hx-vals='{"q":"my music"}'>Load detail</button>
            <div id="recover-fragment"><p>Existing detail</p></div>`;
        document.querySelector('main').append(region); htmx.process(region);
    }''')
    page.get_by_role('button', name='Load detail', exact=True).click()
    target = page.locator('#recover-fragment')
    expect(target).to_have_attribute('aria-busy', 'true')
    expect(target).to_have_text('Existing detail')
    expect(target).to_have_text('Loaded my music', timeout=10000)
    assert len(state.attempts) == 2
    assert state.attempts[1] - state.attempts[0] >= 1.8
    expect(target).not_to_have_attribute('aria-busy', 'true')


def test_expired_mutation_is_never_automatically_replayed(page, recovery_app):
    url, state, _ = recovery_app
    page.goto(url + '/playlists')
    page.evaluate('''() => {
        const button = document.createElement('button'); button.textContent = 'Change library';
        button.setAttribute('hx-post', '/test-mutation');
        document.querySelector('main').append(button); htmx.process(button);
    }''')
    with page.expect_response('**/test-mutation') as response:
        page.get_by_role('button', name='Change library', exact=True).click()
    assert response.value.status == 503
    assert 'x-read-pending' not in response.value.headers
    page.wait_for_timeout(2500)
    assert state.mutations == 1
    expect(page.locator('main h1')).to_have_text('Playlists')


@pytest.mark.parametrize('response', ['deferred', 'success', 'retargeted-success'])
def test_late_timeout_does_not_retry_over_a_newer_fragment(page, recovery_app, response):
    url, _, _ = recovery_app
    page.goto(url + '/playlists')
    held, attempts = [], []

    def respond(route):
        attempts.append(route.request.url)
        if 'q=old' in route.request.url:
            held.append(route)
        else:
            route.fulfill(content_type='text/html', body='<p>Current detail</p>')

    page.route('**/fragment-order?*', respond)
    page.evaluate('''() => {
        const region = document.createElement('section'); region.id = 'detail-region';
        region.innerHTML = `<button hx-get="/fragment-order?q=old" hx-target="#detail-order">Old search</button>
            <button hx-get="/fragment-order?q=current" hx-target="#detail-order">Current search</button>
            <div id="detail-order">Previous detail</div>`;
        document.querySelector('main').append(region); htmx.process(region);
    }''')
    with page.expect_request('**/fragment-order?q=old'):
        page.get_by_role('button', name='Old search', exact=True).click()
    page.get_by_role('button', name='Current search', exact=True).click()
    expect(page.locator('#detail-order')).to_have_text('Current detail')
    assert len(held) == 1
    if response == 'deferred':
        held[0].fulfill(status=503, headers={'X-Read-Pending': 'true', 'Retry-After': '2'})
    else:
        held[0].fulfill(content_type='text/html', body='<p>Obsolete detail</p>',
            headers={'HX-Retarget': '#detail-region'} if response == 'retargeted-success' else {})
    page.wait_for_timeout(2500)
    expect(page.locator('#detail-order')).to_have_text('Current detail')
    assert len(attempts) == 2


@pytest.mark.parametrize('as_request', [False, True], ids=['url', 'request'])
def test_json_fetch_recovers_before_the_panel_consumes_the_response(page, recovery_app, as_request):
    url, state, _ = recovery_app
    state.remaining = 1
    page.goto(url + '/playlists')
    result = page.evaluate('''asRequest => {
        const url = '/test-json-read';
        return fetch(asRequest ? new Request(url) : url).then(response => response.json());
    }''', as_request)
    assert result == {'loaded': True}
    assert len(state.attempts) == 2
    assert state.attempts[1] - state.attempts[0] >= 1.8


def test_aborted_json_read_does_not_retry(page, recovery_app):
    url, state, _ = recovery_app
    state.remaining = 100
    page.goto(url + '/playlists')
    with page.expect_response('**/test-json-read'):
        page.evaluate('''() => {
            window.pendingRead = new AbortController();
            window.readOutcome = fetch('/test-json-read', {signal: pendingRead.signal})
                .then(() => 'loaded', error => error.name);
        }''')
    page.evaluate('pendingRead.abort()')
    assert page.evaluate('readOutcome') == 'AbortError'
    page.wait_for_timeout(2500)
    assert len(state.attempts) == 1


def test_cached_home_restores_reads_actions_and_polling_without_replaying_old_work(
        playwright, browser_type_launch_args, recovery_app):
    url, state, store = recovery_app
    state.remaining = 0
    store.settings.set_setting('onboard_dismissed', '0')
    source = Path(__file__).parents[2] / 'src/yt_playlist/web/static'
    with playwright.chromium.launch(**browser_type_launch_args, channel='chromium',
            ignore_default_args=['--disable-back-forward-cache']) as browser:
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors, requests, assets = [], [], {}
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append(request.url))
        page.on('response', lambda response: assets.update({response.url.split('/')[-1].split('?')[0]: response})
                if '/static/' in response.url else None)
        page.add_init_script('''window.cachedShows = [];
            window.addEventListener('pageshow', event => cachedShows.push(event.persisted));''')
        page.goto(url + '/')
        expect(page.locator('#onboarding-feed')).to_be_visible()
        page.evaluate('''() => {
            window.retainedHome = document.querySelector('#home');
            window.retainedArrival = document.querySelector('#onboarding-feed');
            window.retainedDocument = document;
            window.retainedHomeState = Alpine.$data(retainedHome);
        }''')
        for name in ['read-recovery.js', 'home.js', 'playback-status.js', 'setup-status.js', 'app-navigation.js']:
            assert assets[name].body() == (source / name).read_bytes()
        probe = '''async () => {
            const read = await fetch('/test-json-read').then(r => r.json());
            const write = await fetch('/test-confirmed-mutation', {method: 'POST'}).then(r => r.json());
            return {read, write};
        }'''
        assert page.evaluate(probe) == {'read': {'loaded': True}, 'write': {'saved': 1}}
        session = page.context.new_cdp_session(page)
        for cycle in range(2):
            state.remaining = 100
            with page.expect_response('**/test-json-read'):
                page.evaluate('''() => {
                    window.readOutcome = 'pending';
                    fetch(new Request('/test-json-read'))
                        .then(() => { readOutcome = 'loaded'; }, error => { readOutcome = error.name; });
                }''')
            attempts = len(state.attempts)
            polls = {path: requests.count(url + path) for path in
                     ['/sync/active', '/bridge/status', '/home/onboard/status']}
            # Another origin forces genuine document departure. Browser history
            # must restore that same document, rather than load a replacement.
            page.goto(url.replace('127.0.0.1', 'localhost') + '/playlists')
            history = session.send('Page.getNavigationHistory')
            session.send('Page.navigateToHistoryEntry', {
                'entryId': history['entries'][history['currentIndex'] - 1]['id']})
            deadline = time.monotonic() + 5
            while True:
                result = session.send('Runtime.evaluate', {'returnByValue': True, 'expression': '''({
                    url: location.href, shows: window.cachedShows,
                    same: window.retainedDocument === document,
                    home: window.retainedHome === document.querySelector('#home'),
                    arrival: window.retainedArrival === document.querySelector('#onboarding-feed'),
                    cancelled: window.readOutcome,
                    disposed: window.retainedHome && Alpine.$data(retainedHome)._disposed,
                    suspended: window.retainedHome && Alpine.$data(retainedHome)._suspended
                })'''})
                restored = result.get('result', {}).get('value', {})
                if (restored.get('url') == url + '/' and len(restored.get('shows') or []) == cycle + 2
                        and restored.get('cancelled') == 'AbortError'):
                    break
                assert time.monotonic() < deadline, result
                time.sleep(.05)
            assert restored == {'url': url + '/', 'shows': [False] + [True] * (cycle + 1),
                                'same': True, 'home': True, 'arrival': True,
                                'cancelled': 'AbortError', 'disposed': False, 'suspended': False}
            assert len(state.attempts) == attempts
            state.remaining = 0
            result = session.send('Runtime.evaluate', {'expression': '(' + probe + ')()',
                                  'awaitPromise': True, 'returnByValue': True})
            assert result['result'].get('value') == {
                'read': {'loaded': True}, 'write': {'saved': cycle + 2}}, result
            # These listeners were registered before read recovery. Their first
            # restored poll must succeed without a failed, poisoned request.
            page.wait_for_function('''() => !Alpine.$data(retainedArrival)._requests.signal.aborted
                && window.setupStatus.snapshot && window.playbackStatus.snapshot''')
            for path, count in polls.items():
                assert requests.count(url + path) > count, (path, requests)
            assert state.confirmed_mutations == cycle + 2
            expect(page.locator('#navigation-error, #connection-error, #toasts [role="alert"]')).to_have_count(0)
        page.screenshot(path='/tmp/tuneconsole-865-cached-home-restored-20261106.png', full_page=True)
        page.locator('.topbar').get_by_role('link', name='Playlists', exact=True).click()
        expect(page.locator('main h1')).to_have_text('Playlists')
        assert page.evaluate('document === retainedDocument')
        assert page.evaluate('retainedHomeState._disposed')
        assert not errors


@pytest.mark.parametrize('target', ['url', 'request', 'caller'])
def test_document_exit_aborts_active_retry_transport(page, recovery_app, stalled_routes, target):
    url, state, _ = recovery_app
    state.remaining = 0
    page.goto(url + '/playlists')
    attempts = []

    def transport(route):
        attempts.append(route)
        if len(attempts) == 1:
            route.fulfill(status=503, headers={'X-Read-Pending': 'true', 'Retry-After': '1'})
        # Keep the retry active before headers; cancellation must settle fetch.
        else:
            stalled_routes.append(route)

    page.route('**/active-deferred-read', transport)
    page.evaluate('''target => {
        window.retryCaller = new AbortController();
        const url = '/active-deferred-read';
        const input = target === 'request' ? new Request(url, {signal: retryCaller.signal}) : url;
        const options = target === 'caller' ? {signal: retryCaller.signal} : {};
        window.retryOutcome = 'pending';
        fetch(input, options).then(() => {retryOutcome = 'loaded';},
            error => {retryOutcome = error.name;});
    }''', target)
    try:
        deadline = time.monotonic() + 5
        while len(attempts) < 2:
            assert time.monotonic() < deadline, 'Automatic retry did not start'
            page.wait_for_timeout(50)
        assert page.evaluate('retryOutcome') == 'pending'
        page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        page.wait_for_function("retryOutcome !== 'pending'", timeout=1000)
        assert page.evaluate('retryOutcome') == 'AbortError'
        assert len(attempts) == 2
    finally:
        page.evaluate('retryCaller.abort()')


def test_home_cards_stay_playable_during_a_real_sql_deadline(page, recovery_app, monkeypatch):
    from tests.home_fixture_support import publish_menu, returning_listener
    from yt_playlist.web.routes.home_views import HomeViews

    url, state, store = recovery_app
    state.remaining = 0
    returning_listener(store, 'mixes', now=1)
    publish_menu(store, now=1)
    original = HomeViews.card_context
    reads = []

    def context(views, request, now, **options):
        if request.url.path == '/home/cards':
            reads.append(time.monotonic())
            if len(reads) == 1:
                with views.store.read_view() as reader, work_budget(.005):
                    reader.conn.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL '
                        'SELECT x+1 FROM n WHERE x<100000000) SELECT SUM(x) FROM n').fetchone()
        return original(views, request, now, **options)

    monkeypatch.setattr(HomeViews, 'card_context', context)
    page.set_viewport_size({'width': 1440, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(url + '/')
    card = page.locator('#gen-wheelhouse')
    expect(card).to_be_visible()
    before = card.text_content()
    page.evaluate("htmx.ajax('GET', '/home/cards', {target: '#home-mode-cards', swap: 'outerHTML'})")
    expect(page.locator('#home-mode-cards')).to_have_attribute('aria-busy', 'true')
    expect(card).to_have_text(before)
    card.locator('.gen-head').click(timeout=5000)
    expect(card.get_by_role('button', name='Save and play this mix', exact=True)).to_be_enabled()
    page.screenshot(path='/tmp/tuneconsole-839-home-cards-deferred.png')
    expect(page.locator('#home-mode-cards')).not_to_have_attribute('aria-busy', 'true', timeout=10000)
    expect(card).to_be_visible()
    assert len(reads) == 2 and reads[1] - reads[0] >= 1.8
    assert not errors


def test_playlist_polling_keeps_one_recovering_read_and_cancels_when_leaving(page, recovery_app):
    url, state, store = recovery_app
    state.remaining = 0
    playlist = store.conn.execute('SELECT id FROM playlists LIMIT 1').fetchone()[0]
    attempts = []
    page.add_init_script('''
        const interval = window.setInterval.bind(window);
        window.tablePolls = [];
        window.setInterval = (callback, delay) => {
            if (delay === 2500) tablePolls.push(callback);
            return interval(callback, delay);
        };
    ''')

    def deferred(route):
        attempts.append(route.request.url)
        route.fulfill(status=503, headers={'X-Read-Pending': 'true', 'Retry-After': '10'})

    page.route('**/playlist/*/cells', deferred)
    page.goto(f'{url}/playlist/{playlist}')
    expect(page.locator('tr[data-vid]')).to_have_count(1)
    assert len(attempts) == 1
    page.evaluate('''() => {
        for (let tick = 0; tick < 4; tick++) tablePolls.forEach(callback => callback());
    }''')
    page.wait_for_timeout(100)
    assert len(attempts) == 1, 'Ordinary polling must not accumulate deferred metadata reads'
    page.evaluate("window.dispatchEvent(new Event('app-page-leaving'))")
    page.locator('main').evaluate('(element) => element.remove()')
    # Give the original backoff enough time to fire if disposal failed.
    page.wait_for_timeout(10500)
    assert len(attempts) == 1
