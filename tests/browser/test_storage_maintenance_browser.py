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
from tests.live_server import LiveServer
from tests.conftest import FakeClient
from tests.onboarding_support import publish_personal_mix
from tests.test_storage_maintenance import current_copy_store
from yt_playlist.core import account_auth_state, db_space, playback_feedback
from yt_playlist.core.db_rebuild import build
from yt_playlist.core.storage_maintenance import COPY_LOW_SPACE_MESSAGE
from yt_playlist.web import app as web_app
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
def test_start_radio_during_copy_has_no_error_banner(current_copy_store, page, tmp_path, monkeypatch,
                                                     enough_space, height):
    page.set_viewport_size({'width': 1280, 'height': height})
    store = current_copy_store
    iid = store.accounts.upsert_account('Main', 'bridge', None, True)
    pool = [{'videoId': f'radio-{i}', 'title': f'Radio song {i}',
             'artists': [{'name': f'Artist {i}'}], 'duration_seconds': 180,
             'setVideoId': f'set-radio-{i}'} for i in range(20)]
    tids = [store.tracks.upsert_track(t['videoId'], t['title'], t['artists'][0]['name'], '', 180)
            for t in pool]
    pid = store.playlists.upsert_playlist(iid, 'PLsource', 'Library', len(tids), '', 1)
    store.playlists.set_playlist_tracks(pid, tids)
    store.settings.set_setting('last_sync_at', '1000')
    store.settings.set_setting('onboard_dismissed', '1')
    with store.conn:
        account_auth_state.flag(store.conn, iid, 'Main')
    publish_personal_mix(store, now=1000)
    # A returning listener has already used Radio. Keep the real access
    # projection; this check does not bypass the new-user progression gates.
    store.rec_proposals.put_proposals('listening_choices',
        {'home_format': {'episodes': {'radio': 1}}}, 1000)
    remote = FakeClient(catalog={t['videoId']: t for t in pool})
    app = web_app.create_app(store, lambda: {iid: remote}, now_fn=lambda: 1000, background_work=False)
    ctx = app.state.ctx
    monkeypatch.setattr(ctx.radio, '_start_monitor', lambda s: None)
    monkeypatch.setattr(ctx.rec_worker, 'start_ticker', lambda: None)
    monkeypatch.setattr(ctx.enrich_worker, 'start_ticker', lambda: None)
    monkeypatch.setattr(ctx.rec_worker, 'trigger_mode_refresh', lambda: None)
    monkeypatch.setattr(web_app, '_background_sync_loop', lambda *a, **kw: None)
    monkeypatch.setattr(ctx.radio, 'prepare', lambda: None)
    # The provider handoff is controlled; extension navigation and play events
    # still travel over the real WebSocket below.
    async def confirm_handoff(frame, **kwargs):
        assert frame['type'] == 'radio-session'
        return 200, json.dumps({'ok': True, 'player': ctx.bridge.now_playing,
                               'playback_synced': True, 'native': {'available': True}})
    monkeypatch.setattr(ctx.bridge, 'request_control', confirm_handoff)
    entered, release = threading.Event(), threading.Event()
    compact = build.build

    def hold_copy(source, target, **kwargs):
        entered.set()
        while not release.wait(.01) and not ctx.storage_maintenance._stop.is_set():
            pass
        if not enough_space:
            raise db_space.LowDiskSpace('injected insufficient compaction space')
        return compact(source, target, **kwargs)

    monkeypatch.setattr(build, 'build', hold_copy)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning'))
    owner = LiveServer(server)
    try:
        assert page.request.get(f'http://127.0.0.1:{port}/radio/seeds').json()['tracks']
        assert page.request.get(f'http://127.0.0.1:{port}/home/growth').json()['ready_areas']['radio']
        ctx.storage_maintenance.start_compaction()
        assert entered.wait(10)
        with connect(f'ws://127.0.0.1:{port}/bridge/ws', origin=EXTENSION_ORIGIN, open_timeout=5) as extension, \
                _radio_diagnostics(page, ctx, tmp_path) as diagnostics:
            page.goto(f'http://127.0.0.1:{port}/#radio')
            expect(page.locator('#storage-maintenance')).to_have_text(
                'Optimizing app storage; some recommendations are temporarily unavailable.')
            expect(page.locator('html')).to_have_attribute('data-storage-compacting', 'true')
            expect(page.locator('.authbar')).to_be_hidden()
            start = page.get_by_role('button', name='Start radio with ').first
            expect(start).to_be_visible()
            start.hover(position={'x': 16, 'y': 16})
            expect(page.locator('.radio-seed-info').first).to_have_css('opacity', '1')
            # Use the visible play control; its hit area must stay separate
            # from the song link even when notices leave shallow cover tiles.
            deadline = time.monotonic() + 5
            with page.expect_response(lambda response: urlsplit(response.url).path == '/radio/start'
                                      and response.request.method == 'POST', timeout=5000) as launch:
                start.locator('.radio-seed-play').click(timeout=5000)
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
            page.screenshot(path=f'/tmp/tuneconsole-728-active-{height}-{enough_space}.png')
            release.set()
            assert ctx.storage_maintenance.ready.wait(10)
            playback_feedback.drain(store)
            banner = page.locator('#storage-maintenance')
            expect(banner).to_be_hidden()
            expect(page.locator('html')).to_have_attribute('data-storage-compacting', 'false')
            expect(page.locator('.authbar')).to_be_hidden()
            status = ctx.storage_maintenance.status()
            assert not status['active'] and status['recording']
            assert status['phase'] == ('done' if enough_space else 'deferred')
            assert status['message'] == ('' if enough_space else COPY_LOW_SPACE_MESSAGE)
            assert status['warning'] == (not enough_space)
            assert store.conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
            store.settings.set_setting('after-compaction', 'writes resumed')
            assert store.settings.get_setting('after-compaction') == 'writes resumed'
            page.screenshot(path=f'/tmp/tuneconsole-728-result-{height}-{enough_space}.png')
            # Diagnostics also stay quiet when polling is unavailable.
            page.route('**/static/storage-maintenance.js*', lambda route: route.abort())
            page.reload()
            expect(banner).to_be_hidden()
            page.screenshot(path=f'/tmp/tuneconsole-728-reloaded-{height}-{enough_space}.png')
    finally:
        release.set()
        owner.close()
