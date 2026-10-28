"""Radio and startup notices against a real app while SQLite holds its writer."""
import json
import threading
import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect
import pytest
import uvicorn
from websockets.sync.client import connect

from tests.browser_fixtures import _free_port
from tests.conftest import FakeClient
from tests.core.test_policy_upgrade import _legacy, _state
from tests.onboarding_support import publish_personal_mix
from yt_playlist.core import db_space, policy_upgrade
from yt_playlist.core.storage_maintenance import LOW_SPACE_MESSAGE, MESSAGE
from yt_playlist.web import app as web_app
from yt_playlist.web.context import AUTH_EXPIRED_KEY
from yt_playlist.web.routes.bridge import EXTENSION_ORIGIN


@contextmanager
def _radio_diagnostics(page, ctx, tmp_path):
    started = time.monotonic()
    details = {key: [] for key in ('requests', 'responses', 'frames', 'console', 'errors')}

    def record(key, **event):
        details[key].append({'elapsed': time.monotonic() - started, **event})

    page.on('pageerror', lambda error: record('errors', error=str(error)))
    page.on('console', lambda message: record('console', type=message.type, text=message.text))
    page.on('request', lambda request: record('requests', method=request.method, url=request.url,
                                            body=request.post_data)
            if '/radio/' in request.url or request.is_navigation_request() else None)
    page.on('response', lambda response: record('responses', status=response.status, url=response.url)
            if '/radio/' in response.url else None)
    page.on('requestfailed', lambda request: record('requests', url=request.url, failure=request.failure))
    # Console events survive navigation, including a mistaken click on a song link.
    page.add_init_script("""
        document.addEventListener('click', event => {
            if (!event.target.closest('.radio-seed')) return;
            console.debug('[Repair acceptance click]', JSON.stringify({tag: event.target.tagName,
                link: event.target.closest('a')?.getAttribute('href'),
                button: event.target.closest('button')?.getAttribute('aria-label')}));
        }, true);
    """)
    try:
        yield details
    except Exception as exc:
        details.update(url=page.url, radio=ctx.radio.status(), maintenance=ctx.storage_maintenance.status())
        try:
            details['browser'] = page.evaluate("""() => {
                const root = document.querySelector('.radio-home');
                const radio = root && window.Alpine?.$data(root);
                return {ready: document.readyState, radio: radio && {
                    busy: radio.busy, busyAction: radio.busyAction, loaded: radio.loaded,
                    loading: radio.loading, state: radio.state}};
            }""")
            page.screenshot(path=str(tmp_path / 'radio-launch-failure.png'))
        except Exception as diagnostic_error:
            details['browser_error'] = str(diagnostic_error)
        path = tmp_path / 'radio-launch-diagnostics.json'
        path.write_text(json.dumps(details, indent=2))
        exc.add_note(f'Radio launch diagnostics: {path}\n{json.dumps(details, indent=2)}')
        raise
    else:
        (tmp_path / 'radio-launch-diagnostics.json').write_text(json.dumps(details, indent=2))


@pytest.mark.parametrize('enough_space', [True, False])
@pytest.mark.parametrize('height', [520, 720])
def test_start_radio_during_repair_has_no_error_banner(page, tmp_path, monkeypatch, enough_space, height):
    page.set_viewport_size({'width': 1280, 'height': height})
    store, _ = _legacy(tmp_path / 'state.db')
    iid = store.identities.upsert_identity('Main', 'bridge', None, True)
    pool = [{'videoId': f'radio-{i}', 'title': f'Radio song {i}',
             'artists': [{'name': f'Artist {i}'}], 'duration_seconds': 180,
             'setVideoId': f'set-radio-{i}'} for i in range(20)]
    tids = [store.tracks.upsert_track(t['videoId'], t['title'], t['artists'][0]['name'], '', 180)
            for t in pool]
    pid = store.playlists.upsert_playlist(iid, 'PLsource', 'Library', len(tids), '', 1)
    store.playlists.set_playlist_tracks(pid, tids)
    store.settings.set_setting('last_sync_at', '1000')
    store.settings.set_setting('onboard_dismissed', '1')
    store.settings.set_setting(AUTH_EXPIRED_KEY, json.dumps({str(iid): 'Main'}))
    publish_personal_mix(store, now=1000)
    remote = FakeClient(catalog={t['videoId']: t for t in pool})
    app = web_app.create_app(store, lambda: {iid: remote}, now_fn=lambda: 1000, background_work=True)
    ctx = app.state.ctx
    monkeypatch.setattr(ctx.radio, '_start_monitor', lambda s: None)
    monkeypatch.setattr(ctx.rec_worker, 'start_ticker', lambda: None)
    monkeypatch.setattr(ctx.enrich_worker, 'start_ticker', lambda: None)
    monkeypatch.setattr(ctx.rec_worker, 'trigger_mode_refresh', lambda: None)
    entered, release = threading.Event(), threading.Event()
    compact = policy_upgrade._compact

    def hold_vacuum(conn, directory):
        if not enough_space:
            entered.set()
            while not release.wait(.01) and not ctx.storage_maintenance._stop.is_set():
                pass
            raise db_space.LowDiskSpace('injected insufficient compaction space')
        def progress():
            entered.set()
            while not release.wait(.01) and not ctx.storage_maintenance._stop.is_set():
                pass
            return int(ctx.storage_maintenance._stop.is_set())
        conn.set_progress_handler(progress, 1000)
        compact(conn, directory)
        conn.set_progress_handler(None, 0)

    monkeypatch.setattr(policy_upgrade, '_compact', hold_vacuum)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        assert entered.wait(10)
        with connect(f'ws://127.0.0.1:{port}/bridge/ws', origin=EXTENSION_ORIGIN, open_timeout=5) as extension, \
                _radio_diagnostics(page, ctx, tmp_path) as diagnostics:
            page.goto(f'http://127.0.0.1:{port}/#radio')
            expect(page.locator('#storage-maintenance')).to_have_text(MESSAGE)
            expect(page.locator('#storage-maintenance')).to_be_visible()
            expect(page.locator('.authbar')).to_be_hidden()
            start = page.get_by_role('button', name='Start radio with ').first
            expect(start).to_be_visible()
            start.hover(position={'x': 16, 'y': 16})
            expect(page.locator('.radio-seed-info').first).to_have_css('opacity', '1')
            # The tile also contains a song link, which overlaps its center in
            # short windows. Click the start button's artwork outside that link.
            deadline = time.monotonic() + 5
            with page.expect_response(lambda response: urlsplit(response.url).path == '/radio/start'
                                      and response.request.method == 'POST', timeout=5000) as launch:
                start.click(position={'x': 16, 'y': 16}, timeout=5000)
            response = launch.value
            diagnostics['start_response'] = {'status': response.status, 'body': response.json()}
            assert response.status == 200 and diagnostics['start_response']['body']['ok'], diagnostics
            while True:
                frame = json.loads(extension.recv(timeout=max(0, deadline - time.monotonic())))
                diagnostics['frames'].append(frame)
                if frame != {'ping': 1}:
                    break
            diagnostics['navigation_seconds'] = time.monotonic() - (deadline - 5)
            assert frame['type'] == 'navigate', frame
            query = parse_qs(urlsplit(frame['url']).query)
            assert query['list'] == ['PL_NEW_0']
            video = query['v'][0]
            extension.send(json.dumps({'type': 'play', 'videoId': video, 'title': 'Radio playing',
                                       'artist': 'Artist', 'playlist': 'PL_NEW_0', 'paused': False}))
            expect(page.locator('.radio-session table')).to_be_visible(timeout=10000)
            expect(page.get_by_role('status', name='Starting radio')).to_be_hidden()
            expect(page.locator('.authbar')).to_be_hidden()
            assert ctx.storage_maintenance.active and not ctx.storage_maintenance._stop.is_set()
            diagnostics['during_repair'] = ctx.storage_maintenance.status()
            assert store.settings.get_setting('radio_current_session') is None
            assert store.conn.execute('SELECT count(*) FROM play_events').fetchone()[0] == 0
            assert not diagnostics['errors']
            page.screenshot(path=str(tmp_path / 'active-storage-repair-radio.png'))
            release.set()
            assert ctx.storage_maintenance.ready.wait(10)
            banner = page.locator('#storage-maintenance')
            if enough_space:
                expect(banner).to_be_hidden(timeout=5000)
            else:
                expect(banner).to_have_text(LOW_SPACE_MESSAGE, timeout=5000)
                expect(banner).to_be_visible()
                expect(banner).to_have_attribute('role', 'alert')
                expect(page.locator('html')).to_have_attribute('data-storage-compacting', 'false')
            expect(page.locator('.authbar')).to_be_hidden()
            status = ctx.storage_maintenance.status()
            assert not status['active'] and status['recording']
            assert status['phase'] == ('done' if enough_space else 'deferred')
            assert _state(store.conn)['phase'] == ('done' if enough_space else 'compacting')
            assert status['warning'] == (not enough_space)
            page.screenshot(path=str(tmp_path / 'storage-repair-result.png'))
            # Server rendering preserves a deferred warning even without polling.
            page.route('**/static/storage-maintenance.js*', lambda route: route.abort())
            page.reload()
            if enough_space:
                expect(banner).to_be_hidden()
            else:
                expect(banner).to_be_visible()
                expect(banner).to_have_text(LOW_SPACE_MESSAGE)
            page.screenshot(path=str(tmp_path / 'storage-repair-result-after-reload.png'))
    finally:
        release.set()
        server.should_exit = True
        thread.join(10)
        assert not thread.is_alive()
        store.conn.close()
