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
