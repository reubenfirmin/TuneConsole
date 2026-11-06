"""Alternate dialog retains checked versions across real failed and stalled reads."""
import asyncio
import socket
import threading
from types import SimpleNamespace

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.browser.quiet_action_support import action_evidence, assert_quiet_action
from tests.live_server import LiveServer
from tests.quiet_validation_support import DIAGNOSTIC, prepare_quiet_actions

pytestmark = pytest.mark.browser


@pytest.fixture
def quiet_alternates_app(store, monkeypatch):
    data = prepare_quiet_actions(store, monkeypatch)
    provider = data['main']
    provider._search_results = [{'videoId': 'owned-live', 'title': 'Owned Song (Live)',
        'artists': [{'name': 'Artist & Friends'}], 'duration_seconds': 240}]
    transport = SimpleNamespace(mode=None, entered=threading.Event(), release=threading.Event(), requests=[])

    async def served(scope, receive, send):
        target = scope['type'] == 'http' and scope.get('path', '').endswith('/alternates')
        mode = transport.mode if target else None
        if target:
            transport.requests.append(scope['query_string'])

        async def hold():
            transport.entered.set()
            assert await asyncio.to_thread(transport.release.wait, 10), 'Alternate transport was not released'

        async def bounded_send(message):
            if mode == 'headers' and message['type'] == 'http.response.start':
                await hold()
            if mode == 'body' and message['type'] == 'http.response.body' and message.get('body'):
                body = message['body']
                split = max(1, len(body) // 2)
                await send({**message, 'body': body[:split], 'more_body': True})
                await hold()
                await send({**message, 'body': body[split:]})
                return
            await send(message)

        await data['app'](scope, receive, bounded_send)

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(served, host='127.0.0.1',
            port=listener.getsockname()[1], log_level='warning'))
        with LiveServer(server, sockets=[listener]):
            try:
                yield SimpleNamespace(**data, transport=transport,
                    base=f'http://127.0.0.1:{listener.getsockname()[1]}')
            finally:
                transport.release.set()


def open_selected(page, case):
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.goto(f'{case.base}/playlist/{case.source}')
    page.get_by_role('button', name='More actions for Owned Song').click()
    page.get_by_role('menuitem', name='Find alternate versions').click()
    dialog = page.get_by_role('dialog', name='Alternate versions', exact=True)
    expect(dialog.get_by_text('Owned Song (Live)', exact=True)).to_be_visible()
    dialog.locator('input[name="track"]').check()
    return dialog


@pytest.mark.parametrize('fault', ['storage_value', 'malformed_provider'])
def test_real_alternate_failure_retains_selection_and_recovers_explicitly(
        quiet_alternates_app, page, monkeypatch, fault, action_evidence):
    case = quiet_alternates_app
    before = case.store.charts.playlist_tracks_detail(case.source)
    dialog = open_selected(page, case)
    with monkeypatch.context() as failing:
        if fault == 'storage_value':
            def invalid_account(*args, **kwargs):
                raise ValueError(DIAGNOSTIC)
            failing.setattr(case.store.playlists, 'get_playlist', invalid_account)
        else:
            case.main._search_results = [None]
        with page.expect_response('**/alternates?*') as response:
            dialog.get_by_role('button', name='Search again', exact=True).click()
        assert response.value.status == 503
        expect(dialog.get_by_role('status')).to_have_text('Search not completed. Try again.')
    expect(dialog.locator('input[name="track"]')).to_be_checked()
    expect(dialog.get_by_role('button', name='Add to playlist')).to_be_enabled()
    expect(dialog.get_by_text('Searching YouTube…')).to_be_hidden()
    assert len(case.transport.requests) == 2
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-alternate-' + fault + '.png')
    case.main._search_results = [{'videoId': 'owned-live', 'title': 'Owned Song (Live)',
        'artists': [{'name': 'Artist & Friends'}], 'duration_seconds': 240}]
    with page.expect_response('**/alternates?*') as recovered:
        dialog.get_by_role('button', name='Search again', exact=True).click()
    assert recovered.value.status == 200
    expect(dialog.locator('input[name="track"]')).to_be_checked()
    expect(dialog.get_by_role('status')).to_be_hidden()
    assert len(case.transport.requests) == 3
    assert case.store.charts.playlist_tracks_detail(case.source) == before
    assert not case.main.created and not case.main.added and not case.main.deleted
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-alternate-recovered.png')


@pytest.mark.parametrize('phase', ['headers', 'body'])
def test_stalled_alternate_read_releases_controls_and_retains_checks(
        quiet_alternates_app, page, phase, action_evidence):
    case = quiet_alternates_app
    dialog = open_selected(page, case)
    page.clock.install()
    case.transport.mode = phase
    try:
        dialog.get_by_role('button', name='Search again', exact=True).click()
        assert case.transport.entered.wait(3)
        expect(dialog.get_by_role('button', name='Search again')).to_be_disabled()
        page.clock.fast_forward(20001)
        expect(dialog.get_by_role('button', name='Search again')).to_be_enabled()
        expect(dialog.get_by_role('status')).to_have_text('Search not completed. Try again.')
        expect(dialog.locator('input[name="track"]')).to_be_checked()
        assert len(case.transport.requests) == 2
        assert_quiet_action(page)
        page.screenshot(path=action_evidence + '-alternate-' + phase + '.png')
    finally:
        case.transport.mode = None
        case.transport.release.set()
    dialog.get_by_role('button', name='Cancel', exact=True).click()
    expect(dialog).to_be_hidden()
    assert not case.main.created and not case.main.added and not case.main.deleted


@pytest.mark.parametrize('fault', ['plaintext', 'malformed', 'unrecognized_validation'])
def test_bad_alternate_receipt_keeps_existing_results(quiet_alternates_app, page, fault, action_evidence):
    case = quiet_alternates_app
    dialog = open_selected(page, case)
    receipts = []
    def fail(route):
        receipts.append(True)
        if fault == 'unrecognized_validation':
            route.fulfill(status=422, json={'validation': 'constructor', 'detail': DIAGNOSTIC})
        else:
            route.fulfill(status=503 if fault == 'plaintext' else 200,
                headers={'X-Alternate-Results': 'true'}, content_type='text/html', body=DIAGNOSTIC)
    page.route('**/alternates?*', fail)
    dialog.get_by_role('button', name='Search again', exact=True).click()
    expect(dialog.get_by_role('status')).to_have_text('Search not completed. Try again.')
    expect(dialog.locator('input[name="track"]')).to_be_checked()
    assert receipts == [True]
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-alternate-receipt-' + fault + '.png')


def test_cancelled_alternate_read_cannot_replace_a_newer_dialog(
        quiet_alternates_app, page, action_evidence):
    case = quiet_alternates_app
    dialog = open_selected(page, case)
    case.transport.mode = 'body'
    try:
        dialog.get_by_role('button', name='Search again', exact=True).click()
        assert case.transport.entered.wait(3)
        dialog.get_by_role('button', name='Cancel', exact=True).click()
        expect(dialog).to_be_hidden()
        case.transport.mode = None
        case.main._search_results = [{'videoId': 'new-version', 'title': 'New alternate version',
            'artists': [{'name': 'Artist'}], 'duration_seconds': 240}]
        page.get_by_role('button', name='More actions for Owned Song').click()
        page.get_by_role('menuitem', name='Find alternate versions').click()
        expect(dialog.get_by_text('New alternate version', exact=True)).to_be_visible()
    finally:
        case.transport.release.set()
    expect(dialog.get_by_text('Owned Song (Live)', exact=True)).to_have_count(0)
    expect(dialog.locator('input[name="track"]')).not_to_be_checked()
    expect(dialog.get_by_role('status')).to_be_hidden()
    assert len(case.transport.requests) == 3
    assert not case.main.created and not case.main.added and not case.main.deleted
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-alternate-cancelled-owner.png')
