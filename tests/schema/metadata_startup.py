"""Actual-library startup inspection used by the opt-in metadata acceptance."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

from fastapi.testclient import TestClient
from playwright.sync_api import expect
import uvicorn

from tests.browser_fixtures import _free_port
from tests.live_server import LiveServer
from tests.conftest import FakeClient
from yt_playlist.core import playback_feedback
from yt_playlist.web.app import create_app
from yt_playlist.web.routes.bridge import _now_playing_snapshot


def convert_while_serving(store, request, directory, monkeypatch, migration, report, save):
    started = time.monotonic()
    store.init_schema(defer_metadata=True)
    report['pre_http_schema_seconds'] = time.monotonic() - started
    assert store._schema_recovery_pending
    identity = store.accounts.get_master_account()
    track = store.conn.execute('SELECT t.video_id,t.title,t.artist,t.duration_s FROM catalog_tracks t '
        'WHERE t.video_id IS NOT NULL AND t.duration_s>60 AND NOT EXISTS ('
        "SELECT 1 FROM item_ratings r WHERE r.identity_id=? AND r.video_id=t.video_id "
        "AND r.status IN ('LIKE','DISLIKE')) AND NOT EXISTS ("
        'SELECT 1 FROM playlist_tracks pt JOIN playlists p ON p.id=pt.playlist_id '
        'JOIN tracks liked ON liked.id=pt.track_id '
        "WHERE p.ytm_playlist_id='LM' AND liked.video_id=t.video_id) ORDER BY t.id LIMIT 1",
        (identity.id,)).fetchone()
    assert track is not None, 'The actual library needs an unrated playable track for both rating toggles'
    remote, commands, converted = FakeClient(), [], []
    app = create_app(store, lambda: {identity.id: remote}, now_fn=lambda: 1793289600, background_work=False)
    ctx = app.state.ctx
    ctx.bridge._send = lambda _: None
    ctx.bridge.account_bound_next = True

    def playing():
        ctx.bridge.now_playing = _now_playing_snapshot({'videoId': track['video_id'],
            'title': track['title'], 'artist': track['artist'], 'position': 15, 'duration': track['duration_s'],
            'brandKnown': True, 'brandId': identity.brand_account_id or ''})
        ctx.bridge.now_playing_seen_at = time.monotonic()

    async def control(frame, timeout=10):
        commands.append(frame)
        return 200, '{"ok":true}'

    monkeypatch.setattr(ctx.bridge, 'request_control', control)
    original = migration.convert_legacy
    entered = threading.Event()

    def converting(conn):
        entered.set()
        result = original(conn)
        converted.append(result)
        return result

    monkeypatch.setattr(migration, 'convert_legacy', converting)
    maintenance = ctx.storage_maintenance
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=_free_port(),
                                         lifespan='off', log_level='warning'))
    owner = None
    foreground = ThreadPoolExecutor(1)
    page = request.getfixturevalue('page')
    try:
        started = time.monotonic()
        maintenance.start()
        owner = LiveServer(server, startup_timeout=10)
        report['http_ready_seconds'] = time.monotonic() - started
        client = TestClient(app, base_url='http://127.0.0.1')
        started = time.monotonic()
        home = foreground.submit(client.get, '/').result(timeout=10)
        report['first_home_seconds'] = time.monotonic() - started
        (directory / 'metadata-first-home.html').write_text(home.text)
        save(first_home_status=home.status_code)
        assert home.status_code == 200 and 'class="gen-card-body' in home.text
        assert 'id="storage-maintenance"' in home.text
        assert report['first_home_seconds'] < 10
        assert entered.wait(120), 'Background conversion did not begin'
        started = time.monotonic()
        home = client.get('/')
        report['home_during_conversion_seconds'] = time.monotonic() - started
        assert home.status_code == 200 and 'class="gen-card-body' in home.text
        assert report['home_during_conversion_seconds'] < 3
        assert client.get('/playlists').status_code == 200
        assert client.post('/sync').status_code == 503
        playing()
        page.goto(f'http://127.0.0.1:{server.config.port}/', wait_until='domcontentloaded')
        expect(page.locator('#storage-maintenance')).to_have_text(
            'Optimizing app storage; some recommendations are temporarily unavailable.')
        expect(page.locator('#home-mode-cards .gen-card-body').first).to_be_visible()
        page.screenshot(path=str(directory / 'metadata-startup-pending.png'), full_page=True)
        page.goto(f'http://127.0.0.1:{server.config.port}/track/{track["video_id"]}',
                  wait_until='domcontentloaded')
        expect(page.locator('.track-heart')).to_be_visible()
        observations = []
        for action, desired in (('like', 'LIKE'), ('like', 'INDIFFERENT'),
                                ('dislike', 'DISLIKE'), ('dislike', 'INDIFFERENT')):
            playing()
            started = time.monotonic()
            button = page.locator('.track-heart' if action == 'like' else '.track-skull')
            with page.expect_response(lambda response: response.url.endswith('/now-playing/rate')) as response:
                button.click()
            result = response.value
            elapsed = time.monotonic() - started
            assert result.status == 200 and result.json() == {'ok': True, 'error': ''}
            expect(button).to_have_attribute('aria-pressed', 'false' if desired == 'INDIFFERENT' else 'true')
            expect(page.locator('.track-detail-status')).to_be_hidden()
            assert elapsed < 3
            observations.append({'status': desired, 'seconds': elapsed})
        playing()
        assert client.post('/now-playing/next', json={'videoId': track['video_id']}).json()['ok']
        assert not maintenance.ready.is_set()
        assert playback_feedback.inbox(store).pending() == 6
        page.screenshot(path=str(directory / 'metadata-track-feedback-pending.png'), full_page=True)
        report.update(playback_during_conversion=observations, native_commands=commands,
                      feedback_pending_during_conversion=6)
        save()
        deadline = time.monotonic() + 1800
        while not maintenance.ready.wait(.5):
            assert time.monotonic() < deadline, 'Background conversion did not complete'
        maintenance.join(10)
        assert not store._schema_recovery_pending
        assert playback_feedback.drain(store) == 6
        assert not playback_feedback.inbox(store).pending()
        assert len([frame for frame in commands if frame['type'] == 'next']) == 2
        assert remote.rated == [(track['video_id'], state)
            for state in ('LIKE', 'INDIFFERENT', 'DISLIKE', 'INDIFFERENT')]
        assert len(converted) == 1
        page.reload(wait_until='domcontentloaded')
        expect(page.locator('#storage-maintenance')).to_be_hidden()
        page.screenshot(path=str(directory / 'metadata-startup-complete.png'), full_page=True)
        save(feedback_replayed=6, schema_after_background=store.conn.execute(
            'SELECT max(version) FROM schema_migrations').fetchone()[0])
        return converted[0]
    finally:
        maintenance.stop()
        try:
            maintenance.join(30)
        finally:
            try:
                if owner is not None:
                    owner.close()
            finally:
                foreground.shutdown(wait=True, cancel_futures=True)
