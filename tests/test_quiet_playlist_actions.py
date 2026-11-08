"""Real playlist handlers keep unexpected diagnostics out of their action UI."""
import json
from html import unescape

import pytest
from fastapi.testclient import TestClient

from tests.quiet_validation_support import DIAGNOSTIC, prepare_quiet_actions
from tests.store_fixture_support import bounded_consumer_schema
from yt_playlist.library.playlist_positions import pending_insertions
from yt_playlist.util.action_kinds import ADD_TRACKS


@pytest.fixture(scope='session')
def _pristine_current_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.fixture(autouse=True)
def action_routes(monkeypatch):
    from yt_playlist.web import app as webapp
    from yt_playlist.web.routes import actions, app_update, audio_preview, playlists

    monkeypatch.setattr(webapp, 'build_all', lambda ctx: [
        module.build(ctx) for module in (actions, app_update, audio_preview, playlists)])


@pytest.fixture
def playlist_actions(store, monkeypatch):
    data = prepare_quiet_actions(store, monkeypatch)
    with TestClient(data['app'], base_url='http://127.0.0.1') as http:
        yield {**data, 'http': http}


def diagnostic(kind):
    if kind == 'json':
        raise json.JSONDecodeError(DIAGNOSTIC, '<html>unavailable</html>', 0)
    if kind == 'runtime':
        raise RuntimeError(DIAGNOSTIC)
    raise ValueError(DIAGNOSTIC)


def assert_quiet_failure(response, caplog):
    assert response.status_code == 503 and response.content == b''
    assert response.headers['hx-reswap'] == 'none'
    assert 'hx-refresh' not in response.headers and 'hx-redirect' not in response.headers
    assert DIAGNOSTIC in caplog.text


@pytest.mark.parametrize('kind', ['json', 'value'])
@pytest.mark.parametrize('boundary', ['target', 'source'])
def test_copy_into_repository_failures_are_quiet_without_writes(
        playlist_actions, monkeypatch, kind, boundary, caplog):
    case = playlist_actions
    store = case['store']
    # A same-account destination reaches the actual source-validation lookup.
    target = store.playlists.upsert_playlist(case['main_id'], 'COPY-DEST', 'Copy destination', 0, '', 1)
    before = store.playlists.get_playlists()
    membership = store.playlists.get_playlist_track_ids(case['source'])
    lookup = store.playlists.get_playlist
    looked_up = []
    failing_id = target if boundary == 'target' else case['source']

    def fail(playlist_id):
        looked_up.append(playlist_id)
        if playlist_id == failing_id:
            diagnostic(kind)
        return lookup(playlist_id)

    with monkeypatch.context() as failing:
        failing.setattr(store.playlists, 'get_playlist', fail)
        response = case['http'].post('/playlists/copy-into', data={'ids': case['source'], 'target': target})
    assert_quiet_failure(response, caplog)
    expected = [target] if boundary == 'target' else [target, target, target, case['source']]
    assert looked_up == expected
    assert store.playlists.get_playlists() == before
    assert store.playlists.get_playlist_track_ids(case['source']) == membership
    assert not store.playlists.get_playlist_track_ids(target)
    assert not case['main'].added and not case['other'].added
    assert not case['main'].created and not case['main'].edited and not case['main'].removed
    assert case['http'].get('/playlists').status_code == 200
    assert looked_up == expected
    assert not case['main'].added and not case['other'].added


@pytest.mark.parametrize('kind', ['runtime', 'value', 'json'])
def test_retry_placement_internal_failure_keeps_pending_action_and_does_not_replay(
        playlist_actions, monkeypatch, kind, caplog):
    case, calls = playlist_actions, []
    store, pid = case['store'], case['source']
    plan = {'playlist_id': pid, 'after_video_id': 'owned-song', 'video_ids': ['owned-live']}
    action_id = store.actions.record_action(ADD_TRACKS, '{}', json.dumps(plan), 'pending', '{}', 100)
    before = store.actions.get_action(action_id)
    membership = store.playlists.get_playlist_track_ids(pid)

    def fail(playlist_id):
        calls.append(playlist_id)
        diagnostic(kind)

    with monkeypatch.context() as failing:
        failing.setattr(store.actions, 'get_pending_placements', fail)
        response = case['http'].post(f'/playlist/{pid}/retry-placement')
    assert_quiet_failure(response, caplog)
    assert calls == [pid]
    assert store.actions.get_action(action_id) == before
    assert store.playlists.get_playlist_track_ids(pid) == membership
    assert [action.id for action, _ in pending_insertions(store, pid)] == [action_id]
    assert not case['main'].edited and not case['main'].added
    assert case['http'].get(f'/playlist/{pid}').status_code == 200
    assert calls == [pid] and not case['main'].edited and not case['main'].added


@pytest.mark.parametrize('kind', ['json', 'value'])
def test_add_initial_track_read_failure_is_logged_without_entering_provider(
        playlist_actions, monkeypatch, kind, caplog):
    case = playlist_actions
    before = case['store'].playlists.get_playlist_track_ids(case['source'])
    def fail(playlist_id):
        diagnostic(kind)
    with monkeypatch.context() as failing:
        failing.setattr(case['store'].charts, 'playlist_tracks_detail', fail)
        response = case['http'].post(f"/playlist/{case['source']}/add-tracks",
            data={'track': json.dumps({'videoId': 'owned-live', 'title': 'Live'})})
    assert_quiet_failure(response, caplog)
    assert case['store'].playlists.get_playlist_track_ids(case['source']) == before
    assert not case['main'].added and not case['main'].created


@pytest.mark.parametrize('operation', ['add-tracks', 'retry-placement', 'rename', 'remove-track', 'copy-into'])
@pytest.mark.parametrize('target', ['missing', 'offline'])
def test_known_playlist_and_account_validation_stays_fixed_and_useful(
        playlist_actions, operation, target):
    case = playlist_actions
    pid = 999999 if target == 'missing' else case['offline']
    forms = {'add-tracks': {'track': json.dumps({'videoId': 'owned-live', 'title': 'Live'})},
        'retry-placement': {}, 'rename': {'title': 'Keep this draft'},
        'remove-track': {'video_id': 'owned-song'},
        'copy-into': {'ids': case['source'], 'target': pid}}
    path = '/playlists/copy-into' if operation == 'copy-into' else f'/playlist/{pid}/{operation}'
    response = case['http'].post(path, data=forms[operation])
    assert response.status_code == 422
    assert ('Playlist no longer exists' if target == 'missing' else "That playlist's account isn't connected.") in unescape(response.text)
    assert DIAGNOSTIC not in response.text
    assert not case['main'].added and not case['other'].added
    assert not case['main'].edited and not case['main'].removed


@pytest.mark.parametrize('target', ['system', 'destination'])
def test_copy_destination_restrictions_remain_human_validation(playlist_actions, target):
    case = playlist_actions
    response = case['http'].post('/playlists/copy-into', data={
        'ids': case['source'], 'target': case[target]})
    assert response.status_code == 422
    assert ('Choose a regular playlist.' if target == 'system' else
        'Choose a destination on the same account as the source playlists.') in response.text
    assert not case['main'].added and not case['other'].added
