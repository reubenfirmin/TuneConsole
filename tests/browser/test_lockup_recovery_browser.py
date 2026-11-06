"""The document stays usable when the server silently stops responding."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

import pytest
from playwright.sync_api import expect

from tests.browser.test_read_recovery_browser import recovery_app


@pytest.fixture
def short_deadlines(page, stalled_routes):
    # Exercise the production 20-second deadline without making each negative
    # case wait 20 seconds. Record the requested value independently.
    script = '''(() => {
        const timeout = AbortSignal.timeout.bind(AbortSignal);
        window.requestDeadlines = [];
        AbortSignal.timeout = milliseconds => {
            requestDeadlines.push(milliseconds);
            return timeout(milliseconds === 20000 ? 2000 : milliseconds);
        };
        document.addEventListener('htmx:beforeSend', event => {
            window.htmxDeadline = event.detail.xhr.timeout;
            event.detail.xhr.timeout = Math.min(event.detail.xhr.timeout, 2000);
        });
    })();'''
    page.add_init_script(script)
    # Some fixtures have already opened the page before requesting shortened
    # deadlines. Cover that document as well as future navigations/popups.
    page.evaluate(script)
    yield


def test_navigation_times_out_then_retries_without_replacing_document(
        page, recovery_app, short_deadlines, stalled_routes):
    url, state, _ = recovery_app
    state.remaining = 0
    held = stalled_routes
    loaded = {}
    assets = {'app-navigation.js', 'read-recovery.js', 'components.css', 'song-actions.js',
              'song-actions.css', 'playback-controls.js', 'storage-maintenance.js', 'library-playlists.js'}
    # Reading a body yields inside Playwright's response callback. Capture the
    # responses immediately, then await their bodies after document load.
    page.on('response', lambda response: loaded.update({response.url.split('/')[-1].split('?')[0]: response})
            if response.url.split('/')[-1].split('?')[0] in assets else None)
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.goto(url + '/playlists')
    source = Path(__file__).parents[2] / 'src/yt_playlist/web/static'
    assert {name: response.body() for name, response in loaded.items()} == {
        name: (source / name).read_bytes() for name in assets}
    selection = page.locator('main input[type="checkbox"]').first
    selection.check()
    page.evaluate("window.documentMarker = 'original'")
    page.route('**/artists', lambda route: held.append(route))
    page.locator('.topbar').get_by_role('link', name='Artists', exact=True).click()
    expect(page.locator('main')).not_to_have_attribute('aria-busy', 'true')
    expect(page.locator('#toasts [role="alert"]')).to_have_count(0)
    expect(page.locator('main h1')).to_have_text('Playlists')
    expect(selection).to_be_checked()
    expect(page.locator('main')).not_to_have_attribute('aria-busy', 'true')
    assert page.url == url + '/playlists'
    page.screenshot(path='/tmp/tuneconsole-879-navigation-timeout.png')
    page.wait_for_event('request', predicate=lambda request: request.url == url + '/artists')
    page.wait_for_event('console', predicate=lambda message: '[Navigation] page unavailable' in message.text)
    expect(page.locator('main')).not_to_have_attribute('aria-busy', 'true')
    expect(page.locator('#toasts [role="alert"]')).to_have_count(0)
    assert len(held) == 2
    expect(page.locator('main h1')).to_have_text('Playlists')
    expect(selection).to_be_checked()
    page.screenshot(path='/tmp/tuneconsole-879-continued-failure.png')
    page.unroute('**/artists')
    expect(page.locator('main h1')).to_have_text('Artists', timeout=10000)
    expect(page.locator('#navigation-error')).to_have_count(0)
    expect(page.locator('#connection-error')).to_have_count(0)
    assert page.evaluate('documentMarker') == 'original'
    assert 20000 in page.evaluate('requestDeadlines')


@pytest.mark.parametrize('status', [404, 500])
def test_http_navigation_failure_preserves_content_and_only_retries_transient_status(page, recovery_app, status):
    url, state, _ = recovery_app
    state.remaining = 0
    page.goto(url + '/playlists')
    page.route('**/artists', lambda route: route.fulfill(status=status, content_type='text/html', body='Unavailable'))
    page.locator('.topbar').get_by_role('link', name='Artists', exact=True).click()
    expect(page.locator('main')).not_to_have_attribute('aria-busy', 'true')
    expect(page.locator('main h1')).to_have_text('Playlists')
    expect(page.locator('#toasts [role="alert"]')).to_have_count(0)
    page.unroute_all(behavior='wait')
    if status == 500:
        expect(page.locator('main h1')).to_have_text('Artists', timeout=10000)
    else:
        # A genuine missing destination must not become an endless retry.
        with page.expect_request(lambda request: request.url == url + '/albums'):
            page.locator('.topbar').get_by_role('link', name='Albums', exact=True).click()
        expect(page.locator('main h1')).to_have_text('Albums')


@pytest.mark.parametrize('removed', [False, True])
def test_quiet_reconciliation_retains_only_surviving_selected_playlists(
        page, recovery_app, short_deadlines, stalled_routes, removed):
    url, state, store = recovery_app
    state.remaining = 0
    posts, errors = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def lose_receipt(route):
        stalled_routes.append(route)
        posts.append(route.request.method)
        assert route.fetch().status == 503

    page.route('**/test-mutation', lose_receipt)
    page.goto(url + '/playlists')
    selected = page.locator('main input[type="checkbox"]').first
    selected.check()
    page.evaluate('''() => {
      window.originalDocument = true;
      window.pendingMutation = htmx.ajax('POST', '/test-mutation', {swap: 'none'})
        .catch(() => 'unconfirmed');
    }''')
    assert page.evaluate('pendingMutation') == 'unconfirmed'
    expect(page.locator('#toasts [role="alert"]')).to_have_count(0)
    expect(selected).to_be_checked()
    original = store.playlists.get_playlists()[0]
    entry = store.playlists.get_playlist_track_ids(original.id)[0]
    if removed:
        store.playlists.remove_playlist(original.id)
    added = store.playlists.upsert_playlist(original.account_id, 'ANOTHER', 'Another playlist', 1, 'h', 2)
    store.playlists.set_playlist_tracks(added, [entry])
    expect(page.locator(f'tr[data-playlist-id="{added}"]')).to_be_visible(timeout=10000)
    expect(page.locator('#connection-error, #navigation-error')).to_have_count(0)
    expect(page.locator('main')).not_to_have_attribute('aria-busy', 'true')
    expect(page.locator('main h1')).to_have_text('Playlists')
    expect(page.locator(f'tr[data-playlist-id="{added}"] input').first).not_to_be_checked()
    if removed:
        expect(page.locator(f'tr[data-playlist-id="{original.id}"]')).to_have_count(0)
        expect(page.locator('.pl-actionbar')).to_be_hidden()
    else:
        expect(page.locator(f'tr[data-playlist-id="{original.id}"] input').first).to_be_checked()
        expect(page.locator('.pl-actionbar')).to_be_visible()
        expect(page.locator('.pl-actionbar strong')).to_have_text('1 selected')
    assert page.evaluate('originalDocument')
    assert page.evaluate('pendingMutation') == 'unconfirmed'
    assert state.mutations == 1 and posts == ['POST']
    assert 20000 in page.evaluate('requestDeadlines')
    page.screenshot(path=f'/tmp/tuneconsole-879-reconnected-{removed}.png')
    # Selection is recovery context, not a persistent preference for a future
    # visit or another page. Ordinary navigation starts a fresh selection.
    page.locator('.topbar').get_by_role('link', name='Artists', exact=True).click()
    expect(page.locator('main h1')).to_have_text('Artists')
    page.locator('.topbar').get_by_role('link', name='Playlists', exact=True).click()
    expect(page.locator('main h1')).to_have_text('Playlists')
    expect(page.locator('.pl-actionbar')).to_be_hidden()
    assert not errors


@pytest.mark.parametrize('failure', ['timeout', 'network', 'http'])
def test_htmx_failure_releases_disabled_control_and_recovers_quietly(
        page, recovery_app, short_deadlines, stalled_routes, failure):
    url, state, _ = recovery_app
    state.remaining = 0
    page.goto(url + '/playlists')
    held = stalled_routes
    attempts = []
    def fail(route):
        attempts.append(route)
        if failure == 'network':
            route.abort('failed')
        elif failure == 'http':
            route.fulfill(status=503, body='Unavailable')
        else:
            held.append(route)

    page.route('**/test-fragment?*', fail)
    page.evaluate('''() => {
        const region = document.createElement('section');
        region.innerHTML = `<button hx-get="/test-fragment?q=music" hx-target="#stalled-detail"
            hx-disabled-elt="this">Load detail</button><div id="stalled-detail">Existing detail</div>`;
        document.querySelector('main').append(region); htmx.process(region);
    }''')
    button = page.get_by_role('button', name='Load detail', exact=True)
    button.click()
    if failure == 'timeout':
        expect(button).to_be_disabled()
    expect(button).to_be_enabled()
    expect(button).not_to_have_class('htmx-request')
    expect(page.locator('#stalled-detail')).to_have_text('Existing detail')
    assert page.evaluate('htmxDeadline') == 20000
    assert len(attempts) == 1
    page.unroute('**/test-fragment?*')
    expect(page.locator('#stalled-detail')).to_have_text('Loaded music', timeout=10000)
    expect(page.locator('#connection-error')).to_have_count(0)
    expect(page.locator('main h1')).to_have_text('Playlists')
    expect(page.locator('#toasts [role="alert"]')).to_have_count(0)
    expect(button).to_be_enabled()


@pytest.mark.parametrize('method,path', [
    ('GET', '/silent-request'), ('POST', '/silent-request'),
    ('GET', '/bridge/status'), ('GET', '/storage-maintenance/status'),
])
def test_silent_fetch_settles_and_never_replays_a_write(
        page, recovery_app, short_deadlines, stalled_routes, method, path):
    url, _, _ = recovery_app
    page.goto(url + '/playlists')
    held = stalled_routes
    reads = []
    page.on('request', lambda request: reads.append(request.url)
            if request.url == url + '/playlists' else None)
    selected = page.locator('main input[type="checkbox"]').first
    selected.check()
    page.evaluate('''() => {
        const input = document.createElement('input');
        input.id = 'unsaved-draft'; input.value = 'Still editing';
        document.querySelector('main').append(input);
        window.unresolvedAction = {busy: true};
    }''')
    page.route('**' + path, lambda route: held.append(route))
    page.evaluate('''({method, path}) => {
        window.requestBusy = true;
        window.requestOutcome = fetch(path, {method}).then(r => r.json())
            .then(() => 'loaded', error => error.name).finally(() => {requestBusy = false;});
    }''', {'method': method, 'path': path})
    assert page.evaluate('requestOutcome') == 'TimeoutError'
    assert page.evaluate('requestBusy') is False
    page.wait_for_timeout(2300)
    assert reads == [], 'a background/control read must not reconcile the whole page'
    expect(page.locator('#unsaved-draft')).to_have_value('Still editing')
    expect(selected).to_be_checked()
    assert page.evaluate('unresolvedAction.busy') is True
    expect(page.locator('#connection-error')).to_have_count(0)
    if path == '/silent-request':
        assert len(held) == 1


def test_stalled_response_body_times_out_and_next_request_works(page, recovery_app, short_deadlines):
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', '15')
            self.end_headers()
            self.wfile.write(b'{"loaded":')
            self.wfile.flush()
            release.wait(5)
            try:
                self.wfile.write(b'true}')
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url, state, _ = recovery_app
        state.remaining = 0
        page.goto(url + '/playlists')
        outcome = page.evaluate('''url => fetch(url).then(response => response.json())
            .then(() => 'loaded', error => error.name)''', f'http://127.0.0.1:{server.server_port}/body')
        # Fetch resolved its headers; the response body still has a deadline.
        assert outcome in ('AbortError', 'TimeoutError')
        expect(page.locator('#toasts [role="alert"]')).to_have_count(0)
        assert page.evaluate("fetch('/test-json-read').then(response => response.json())") == {'loaded': True}
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.mark.parametrize('phase', ['active', 'backoff'])
def test_new_navigation_cancels_stalled_page_and_clears_busy_state(
        page, recovery_app, short_deadlines, stalled_routes, phase):
    url, _, _ = recovery_app
    page.goto(url + '/playlists')
    held = stalled_routes
    page.route('**/artists', lambda route: held.append(route))
    page.evaluate("navigation.navigate('/artists')")
    page.wait_for_function("document.querySelector('main').getAttribute('aria-busy') === 'true'")
    if phase == 'backoff':
        expect(page.locator('main')).not_to_have_attribute('aria-busy', 'true')
    page.locator('.topbar').get_by_role('link', name='Albums', exact=True).click()
    expect(page.locator('main h1')).to_have_text('Albums')
    expect(page.locator('main')).not_to_have_attribute('aria-busy', 'true')
    expect(page.locator('#navigation-error')).to_have_count(0)
    expect(page.locator('#connection-error')).to_have_count(0)
    # The former destination cannot return when its backoff would have fired.
    page.wait_for_timeout(2500)
    expect(page.locator('main h1')).to_have_text('Albums')
    assert len(held) == 1


def test_form_timeout_refreshes_with_get_without_resubmitting(
        page, recovery_app, short_deadlines, stalled_routes):
    url, _, _ = recovery_app
    page.goto(url + '/playlists')
    held = stalled_routes
    page.route('**/uncertain-form', lambda route: held.append(route))
    page.evaluate('''() => {
        const form = document.createElement('form');
        form.method = 'POST'; form.action = '/uncertain-form';
        form.innerHTML = '<button>Make change</button>';
        document.querySelector('main').append(form);
    }''')
    page.get_by_role('button', name='Make change', exact=True).click()
    expect(page.locator('main')).to_have_attribute('aria-busy', 'true')
    expect(page.locator('main')).not_to_have_attribute('aria-busy', 'true')
    assert len(held) == 1 and held[0].request.method == 'POST'
    expect(page.get_by_role('button', name='Make change', exact=True)).to_have_count(0, timeout=10000)
    expect(page.locator('#navigation-error')).to_have_count(0)
    expect(page.locator('main h1')).to_have_text('Playlists')
    assert len(held) == 1
