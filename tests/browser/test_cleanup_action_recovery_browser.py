"""Actual cleanup controls keep action ownership through quiet reconciliation."""
import json
import socket
from functools import partial

import pytest
import uvicorn
from playwright.sync_api import expect

from tests.browser.quiet_action_support import action_evidence, assert_quiet_action
from tests.conftest import FakeClient, _track
from tests.live_server import LiveServer
from tests.quiet_validation_support import DIAGNOSTIC
from yt_playlist.library import executor
from yt_playlist.util.retry import with_retry
from yt_playlist.web.app import create_app

pytestmark = pytest.mark.browser


@pytest.fixture
def cleanup_actions(store, monkeypatch):
    account = store.accounts.upsert_account('Main', 'bridge', None, True)
    track = store.tracks.upsert_track('one', 'One song', 'Artist', None, 200)
    ids = {}
    for remote, title, count in (('KEEP', 'Keeper', 1), ('COPY', 'Copy', 1), ('EMPTY', 'Empty entry', 0)):
        ids[remote] = store.playlists.upsert_playlist(account, remote, title, count, '', 1)
        store.playlists.set_playlist_tracks(ids[remote], [track] if count else [])
    client = FakeClient(tracks={remote: [_track('one', 'One song', 'Artist')]
                                for remote in ('KEEP', 'COPY')})
    monkeypatch.setattr(executor, 'with_retry', partial(with_retry, sleep=lambda delay: None))
    app = create_app(store, lambda: {account: client}, now_fn=lambda: 2, background_work=False)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level='warning'))
        with LiveServer(server, sockets=[listener]):
            yield {'store': store, 'client': client, 'ids': ids,
                   'base': f'http://127.0.0.1:{listener.getsockname()[1]}'}


def fail_read(*args, **kwargs):
    raise ValueError(DIAGNOSTIC)


def capture_controls(page, control, path):
    page.locator('#app-page').evaluate('''node => Promise.all(node.getAnimations({subtree: true})
        .filter(animation => animation.effect.getTiming().iterations !== Infinity)
        .map(animation => animation.finished.catch(() => {})))''')
    control.scroll_into_view_if_needed()
    page.screenshot(path=path)


def empty_removal(page, case, monkeypatch):
    with monkeypatch.context() as failing:
        failing.setattr(case['client'], 'get_playlist', fail_read)
        with page.expect_response('**/cleanup'):
            with page.expect_response('**/playlist/delete-empty') as response:
                page.get_by_role('row').filter(has_text='Empty entry').get_by_role(
                    'button', name='Delete', exact=True).click()
        assert response.value.status == 503
        removal = page.get_by_role('button', name='Remove from TuneConsole', exact=True)
        expect(removal).to_be_visible()
    return removal


@pytest.mark.parametrize('kind', ['json', 'value'])
@pytest.mark.parametrize('operation', ['keep-one', 'delete-empty', 'forget-empty'])
def test_cleanup_lookup_failure_releases_real_control_and_only_reconciles_with_get(
        cleanup_actions, page, monkeypatch, kind, operation, action_evidence, caplog):
    case, store = cleanup_actions, cleanup_actions['store']
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.goto(case['base'] + '/cleanup')
    page.evaluate('window.cleanupDocument = document')
    if operation == 'forget-empty':
        empty_removal(page, case, monkeypatch)
    playlists, actions = store.playlists.get_playlists(), store.actions.get_actions()
    tracks = {pid: store.charts.playlist_tracks_detail(pid) for pid in case['ids'].values()}
    lookup, posts, errors = [], [], []
    original = store.playlists.get_playlist

    def lookup_failure(pid):
        if not lookup:
            lookup.append(pid)
            if kind == 'json':
                raise json.JSONDecodeError(DIAGNOSTIC, '<html>unavailable</html>', 0)
            raise ValueError(DIAGNOSTIC)
        return original(pid)

    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: posts.append(request.url) if request.method == 'POST'
            and any(path in request.url for path in ('/dupe/', '/playlist/')) else None)
    monkeypatch.setattr(store.playlists, 'get_playlist', lookup_failure)
    if operation == 'keep-one':
        path = '/dupe/keep-one'
        control = page.get_by_role('row').filter(has_text='Keeper').get_by_role('button', name='Keep this one')
        pid = case['ids']['KEEP']
    else:
        path = '/playlist/' + operation
        pid = case['ids']['EMPTY']
        control = page.get_by_role('row').filter(has_text='Empty entry').get_by_role(
            'button', name='Delete' if operation == 'delete-empty' else 'Remove from TuneConsole', exact=True)
    with page.expect_response('**/cleanup'):
        with page.expect_response('**' + path) as response:
            control.click()
            if operation == 'forget-empty':
                page.get_by_role('button', name='Confirm', exact=True).click()
        assert response.value.status == 503 and response.value.body() == b''
    expect(page.get_by_role('row').filter(has_text='Empty entry')).to_be_visible()
    expect(page.get_by_role('row').filter(has_text='Keeper').first).to_be_visible()
    expect(page.get_by_role('row').filter(has_text='Copy').first).to_be_visible()
    expect(control).to_be_enabled()
    # The action's retained indication is local to its controls after the GET.
    notice = "Cleanup isn't complete." if operation == 'keep-one' else "This change isn't confirmed."
    expect(control.locator('..').get_by_text(notice, exact=False)).to_be_visible()
    if operation == 'delete-empty':
        expect(page.get_by_role('button', name='Remove from TuneConsole', exact=True)).to_be_visible()
    assert page.evaluate('window.cleanupDocument === document')
    assert lookup == [pid] and len(posts) == 1 and path in posts[0]
    assert DIAGNOSTIC in caplog.text
    assert store.playlists.get_playlists() == playlists and store.actions.get_actions() == actions
    assert {pid: store.charts.playlist_tracks_detail(pid) for pid in tracks} == tracks
    assert not case['client'].deleted and not case['client'].added and not case['client'].created
    assert not errors
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-cleanup-' + operation + '-' + kind + '.png', full_page=True)
    # Ordinary navigation ends this ephemeral action indication.
    page.get_by_role('link', name='Playlists', exact=True).first.click()
    page.wait_for_url('**/playlists')
    page.goto(case['base'] + '/cleanup')
    expect(page.locator('.action-outcome:visible').filter(has_text=notice)).to_have_count(0)
    assert len(posts) == 1


@pytest.mark.parametrize('operation', ['keep-one', 'delete-empty'])
def test_lost_cleanup_delete_receipt_keeps_saved_attempt_and_never_replays(
        cleanup_actions, page, monkeypatch, operation, action_evidence):
    case, store, client = cleanup_actions, cleanup_actions['store'], cleanup_actions['client']
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.goto(case['base'] + '/cleanup')
    attempts, posts = [], []
    original = client.delete_playlist

    def lose_receipt(remote):
        attempts.append(remote)
        original(remote)
        raise TimeoutError(DIAGNOSTIC)

    monkeypatch.setattr(client, 'delete_playlist', lose_receipt)
    page.on('request', lambda request: posts.append(request.url) if request.method == 'POST'
            and any(path in request.url for path in ('/dupe/', '/playlist/')) else None)
    path = '/dupe/keep-one' if operation == 'keep-one' else '/playlist/delete-empty'
    title, button = ('Keeper', 'Keep this one') if operation == 'keep-one' else ('Empty entry', 'Delete')
    with page.expect_response('**/cleanup'):
        with page.expect_response('**' + path) as response:
            page.get_by_role('row').filter(has_text=title).get_by_role('button', name=button, exact=True).click()
        assert response.value.status == 503
    expect(page.get_by_role('row').filter(has_text=title).first).to_be_visible()
    action, = store.actions.get_actions()
    assert action.status == 'delete_pending'
    deletion, = json.loads(action.undo_json)['deletions']
    assert deletion['phase'] == 'uncertain'
    from yt_playlist.core import fao
    backup = fao.read_bytes(deletion['backup'])
    assert attempts == client.deleted == ['COPY' if operation == 'keep-one' else 'EMPTY']
    assert len(posts) == 1
    page.reload()
    expect(page.get_by_role('row').filter(has_text=title).first).to_be_visible()
    assert len(posts) == 1 and len(attempts) == 1
    assert store.actions.get_action(action.id).undo_json == action.undo_json
    assert fao.read_bytes(deletion['backup']) == backup
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-cleanup-uncertain-' + operation + '.png', full_page=True)


def test_completed_local_removal_with_lost_receipt_is_only_read_back(
        cleanup_actions, page, monkeypatch, action_evidence):
    case, store = cleanup_actions, cleanup_actions['store']
    page.goto(case['base'] + '/cleanup')
    empty_removal(page, case, monkeypatch)
    posts = []

    def lose_receipt(route):
        posts.append(route.request.url)
        response = route.fetch()
        assert response.status == 200
        route.fulfill(status=503, body=DIAGNOSTIC)

    page.route('**/playlist/forget-empty', lose_receipt)
    with page.expect_response('**/cleanup'):
        page.get_by_role('button', name='Remove from TuneConsole', exact=True).click()
        page.get_by_role('button', name='Confirm', exact=True).click()
    expect(page.get_by_role('row').filter(has_text='Empty entry')).to_have_count(0)
    assert store.playlists.get_playlist(case['ids']['EMPTY']) is None
    action, = store.actions.get_actions()
    assert action.kind == 'remove_local_playlist'
    assert json.loads(action.params_json)['reason'] == 'user_requested'
    assert not case['client'].deleted and len(posts) == 1
    page.reload()
    expect(page.get_by_role('row').filter(has_text='Empty entry')).to_have_count(0)
    assert [item.id for item in store.actions.get_actions()] == [action.id] and len(posts) == 1
    assert_quiet_action(page)
    page.screenshot(path=action_evidence + '-local-removal-reconciled.png', full_page=True)


def test_failed_reconciliation_keeps_local_removal_until_a_later_get_succeeds(
        cleanup_actions, page, monkeypatch, action_evidence):
    case, client = cleanup_actions, cleanup_actions['client']
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.goto(case['base'] + '/cleanup')
    monkeypatch.setattr(client, 'get_playlist', fail_read)
    reads, posts = [], []

    def unavailable_once(route):
        reads.append(route.request.url)
        if len(reads) == 1:
            route.fulfill(status=503, body=DIAGNOSTIC)
        else:
            route.continue_()

    page.route('**/cleanup', unavailable_once)
    page.on('request', lambda request: posts.append(request.url) if request.method == 'POST'
            and '/playlist/' in request.url else None)
    removal = page.get_by_role('button', name='Remove from TuneConsole', exact=True)
    with page.expect_response(lambda response: response.url.endswith('/cleanup') and response.status == 200):
        with page.expect_response(lambda response: response.url.endswith('/cleanup') and response.status == 503):
            page.get_by_role('row').filter(has_text='Empty entry').get_by_role('button', name='Delete', exact=True).click()
        expect(removal).to_be_visible()
        assert_quiet_action(page)
        capture_controls(page, removal, action_evidence + '-reconciliation-unavailable.png')
    expect(removal).to_be_visible()
    expect(page.get_by_text("This change isn't confirmed.", exact=True)).to_be_visible()
    assert len(reads) == 2 and len(posts) == 1 and not client.deleted
    assert_quiet_action(page)
    capture_controls(page, removal, action_evidence + '-reconciliation-restored.png')
    # Failure in the explicit local-removal action retains that same control.
    original = case['store'].playlists.get_playlist
    failed = []

    def fail_lookup(pid):
        if not failed:
            failed.append(pid)
            raise ValueError(DIAGNOSTIC)
        return original(pid)

    monkeypatch.setattr(case['store'].playlists, 'get_playlist', fail_lookup)
    with page.expect_response('**/cleanup'):
        removal.click()
        page.get_by_role('button', name='Confirm', exact=True).click()
    expect(removal).to_be_visible()
    expect(removal).to_be_enabled()
    assert len(posts) == 2 and not client.deleted
    assert_quiet_action(page)
    capture_controls(page, removal, action_evidence + '-local-removal-unconfirmed.png')


def test_keep_one_verified_missing_tracks_stays_beside_its_control(
        cleanup_actions, page, action_evidence):
    case, store, client = cleanup_actions, cleanup_actions['store'], cleanup_actions['client']
    client._tracks['KEEP'] = []  # Remote keeper changed after the cached duplicate analysis.
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.goto(case['base'] + '/cleanup')
    control = page.get_by_role('row').filter(has_text='Keeper').get_by_role('button', name='Keep this one')
    with page.expect_response('**/dupe/keep-one') as response:
        control.click()
    assert response.value.status == 422
    outcome = page.locator(f"#keep-outcome-{case['ids']['KEEP']}")
    expect(outcome).to_contain_text('missing 1 required track')
    expect(outcome).to_be_visible()
    expect(control).to_be_enabled()
    assert store.playlists.get_playlist(case['ids']['COPY']) is not None and not client.deleted
    assert_quiet_action(page)
    capture_controls(page, outcome, action_evidence + '-keep-one-missing-tracks.png')
