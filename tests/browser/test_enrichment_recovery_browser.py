"""Enrichment retains useful rows across real failures, replay and bounded transport waits."""
import asyncio
import socket
import threading
from types import SimpleNamespace

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.browser.quiet_action_support import action_evidence, assert_quiet_action
from tests.live_server import LiveServer
from tests.quiet_enrichment_support import quiet_enrichment
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.browser


@pytest.fixture
def quiet_enrichment_app(store, quiet_enrichment):
    case = quiet_enrichment
    app = create_app(store, lambda: case.clients, now_fn=lambda: case.now)
    transport = SimpleNamespace(mode=None, entered=threading.Event(), release=threading.Event())

    async def served(scope, receive, send):
        path = scope.get('path', '')
        mode = transport.mode
        target = (scope['type'] == 'http' and
            ((mode in ('start_headers', 'start_body') and scope['method'] == 'POST' and path.endswith('/enrich'))
             or (mode in ('stream_headers', 'stream_idle') and path.startswith('/playlist/enrich/events/'))))

        async def hold():
            transport.entered.set()
            assert await asyncio.to_thread(transport.release.wait, 10), 'Transport was not released'

        async def bounded_send(message):
            if target and message['type'] == 'http.response.start' and mode.endswith('headers'):
                await hold()
            if target and message['type'] == 'http.response.body' and message.get('body'):
                if mode == 'start_body':
                    body = message['body']
                    split = max(1, len(body) // 2)
                    await send({**message, 'body': body[:split], 'more_body': True})
                    await hold()
                    await send({**message, 'body': body[split:]})
                    return
                if mode == 'stream_idle' and not transport.entered.is_set():
                    await send(message)
                    await hold()
                    return
            await send(message)

        await app(scope, receive, bounded_send)

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(served, host='127.0.0.1',
                                               port=listener.getsockname()[1], log_level='warning'))
        with LiveServer(server, sockets=[listener]):
            try:
                yield SimpleNamespace(base=f'http://127.0.0.1:{listener.getsockname()[1]}',
                                      app=app, case=case, transport=transport)
            finally:
                case.release.set()
                transport.release.set()


def open_enrichment(page, live, scope):
    page.set_viewport_size({'width': 1280, 'height': 900})
    path = f'/playlist/{live.case.playlist}' if scope == 'playlist' else '/album?browse=QUIET-ALBUM'
    page.goto(live.base + path)
    expect(page.locator('tr.srow[data-vid="quiet-0"]')).to_be_visible()
    return page.get_by_role('button', name='Enrich', exact=True)


@pytest.mark.parametrize('scope', ['playlist', 'album'])
@pytest.mark.parametrize('fault', ['outer', 'provider', 'artwork', 'deferred'])
def test_enrichment_failure_keeps_rows_and_explicit_recovery(
        quiet_enrichment_app, store, page, scope, fault, action_evidence):
    live, case = quiet_enrichment_app, quiet_enrichment_app.case
    case.fault = fault
    case.release.clear()
    button = open_enrichment(page, live, scope)
    try:
        button.click()
        assert case.observed.wait(3)
        status = page.locator('.enrich-status')
        expect(status).to_have_text('1 of 2 songs checked.' if fault == 'outer'
                                   else 'Some details remain for later. Checking the rest…')
        expect(button).to_be_disabled()
        assert_quiet_action(page)
        page.screenshot(path=action_evidence + '-enrichment-progress.png')
        page.reload()  # Both views rejoin the same active worker, never POST a second job.
        expect(button).to_be_disabled()
        expect(status).to_have_text('1 of 2 songs checked.' if fault == 'outer'
                                   else 'Some details remain for later. Checking the rest…')
        assert len(case.calls) == 1
        assert_quiet_action(page)
    finally:
        case.release.set()
    expect(button).to_be_enabled()
    assert case.finished.wait(3)
    assert len(case.calls) == 1
    row = page.locator('tr.srow[data-vid="quiet-0"]')
    expect(row).to_be_visible()
    if fault in ('outer', 'artwork'):
        expect(row).to_have_attribute('data-genre', 'rock')
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-enrichment-finished.png')
    store.tracks.set_track_genre(case.tracks[0], 'Jazz')
    store.tracks.set_track_year(case.tracks[0], '1999')
    page.reload()
    expect(row).to_have_attribute('data-genre', 'jazz')
    expect(row).to_have_attribute('data-year', '1999')
    assert len(case.calls) == 1
    assert_quiet_action(page)
    case.fault = None
    case.now += 3600  # Retain real retry admission; advance the controlled provider clock.
    button.click()
    expect(button).to_be_enabled()
    expect(page.locator('.enrich-status')).to_contain_text('Checked')
    assert len(case.calls) == 2
    expect(row).to_have_attribute('data-genre', 'jazz')
    expect(row).to_have_attribute('data-year', '1999')
    expect(page.locator('tr.srow[data-vid="quiet-1"]')).to_have_attribute('data-genre', 'rock')
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-enrichment-recovered.png')


@pytest.mark.parametrize('phase', ['start_headers', 'start_body', 'stream_headers', 'stream_idle'])
def test_stalled_enrichment_releases_controls_without_replaying_post(
        quiet_enrichment_app, store, page, phase, action_evidence):
    live, case = quiet_enrichment_app, quiet_enrichment_app.case
    button = open_enrichment(page, live, 'album')
    page.clock.install()
    live.transport.mode = phase
    try:
        button.click()
        assert live.transport.entered.wait(3)
        expect(button).to_be_disabled()
        assert len(case.calls) == 1
        page.clock.fast_forward(20001)
        expect(button).to_be_enabled()
        expect(page.locator('.enrich-status')).to_have_text(
            'Your current details are kept. You can check enrichment again.')
        assert_quiet_action(page)
        page.screenshot(path=action_evidence + '-enrichment-' + phase + '.png')
        assert len(case.calls) == 1
    finally:
        live.transport.release.set()
        live.transport.mode = None
    assert case.finished.wait(3)
    page.reload()
    expect(button).to_be_enabled()
    expect(page.locator('tr.srow[data-vid="quiet-0"]')).to_have_attribute('data-genre', 'rock')
    expect(page.locator('tr.srow[data-vid="quiet-1"]')).to_have_attribute('data-genre', 'rock')
    assert len(case.calls) == 1
    assert all(store.tracks.get_track_enrichment(track) == ('Rock', '2001') for track in case.tracks)
    assert_quiet_action(page)


@pytest.mark.parametrize('fault', ['http', 'malformed_start', 'disconnect', 'malformed_event', 'diagnostic_events'])
def test_enrichment_rejects_bad_receipts_without_replaying_the_worker(
        quiet_enrichment_app, store, page, fault, action_evidence):
    import json

    live, case = quiet_enrichment_app, quiet_enrichment_app.case
    if fault in ('http', 'malformed_start', 'disconnect'):
        def bad_start(route):
            result = route.fetch()  # The genuine worker may have committed before receipt fails.
            assert result.ok
            if fault == 'disconnect':
                route.abort()
            else:
                route.fulfill(status=503 if fault == 'http' else 200,
                              content_type='application/json', body=case.diagnostic)
        page.route('**/album/QUIET-ALBUM/enrich', bad_start)
    else:
        def bad_events(route):
            raw = (case.diagnostic + '\n\n' if fault == 'malformed_event' else
                ''.join('data: ' + json.dumps(event) + '\n\n' for event in [
                    {'type': 'info', 'warning': True, 'text': case.diagnostic},
                    {'type': 'err', 'text': 'Last.fm API key: ' + case.diagnostic},
                    {'type': 'end', 'error': case.diagnostic}]))
            if fault == 'malformed_event':
                raw = 'data: ' + raw
            route.fulfill(content_type='text/event-stream', body=raw)
        page.route('**/playlist/enrich/events/*', bad_events)
    button = open_enrichment(page, live, 'album')
    button.click()
    expect(page.locator('.enrich-status')).to_have_text(
        'Your current details are kept. You can check enrichment again.')
    expect(button).to_be_enabled()
    assert case.finished.wait(3)
    assert len(case.calls) == 1
    assert all(store.tracks.get_track_enrichment(track) == ('Rock', '2001') for track in case.tracks)
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-enrichment-' + fault + '.png')
    page.reload()
    expect(page.locator('tr.srow[data-vid="quiet-0"]')).to_have_attribute('data-genre', 'rock')
    expect(page.locator('tr.srow[data-vid="quiet-1"]')).to_have_attribute('data-genre', 'rock')
    assert len(case.calls) == 1
    assert_quiet_action(page)
