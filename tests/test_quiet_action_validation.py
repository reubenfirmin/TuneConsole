"""Provider ValueError/JSONDecodeError must never become human validation UI."""
import json
import pytest
from fastapi.testclient import TestClient

from tests.quiet_validation_support import DIAGNOSTIC, prepare_quiet_actions
from tests.test_song_actions import SONG


@pytest.fixture
def quiet_actions(store, monkeypatch):
    data = prepare_quiet_actions(store, monkeypatch)
    with TestClient(data['app'], base_url='http://127.0.0.1') as http:
        yield {**data, 'http': http}


@pytest.mark.parametrize('kind,after', [('value', False), ('json', True)])
def test_rating_provider_error_is_quiet_without_local_confirmation(quiet_actions, kind, after, caplog):
    case, provider = quiet_actions, quiet_actions['main']
    provider.fault, provider.kind, provider.after = 'rate_song', kind, after
    before = case['store'].tracks.item_rating(case['main_id'], 'owned-song')
    response = case['http'].post('/track/like', data={'video_id': 'owned-song', 'on': '1'})
    assert response.status_code == 503 and response.content == b''
    assert response.headers['hx-reswap'] == 'none'
    assert DIAGNOSTIC in caplog.text
    assert case['store'].tracks.item_rating(case['main_id'], 'owned-song') == before
    assert provider.calls == ['rate_song'] * 3  # Existing bounded backend attempts, no UI replay.
    assert provider.rated == ([('owned-song', 'LIKE')] * 3 if after else [])
    assert case['http'].get(f"/playlist/{case['source']}").status_code == 200
    assert provider.calls == ['rate_song'] * 3
    provider.fault = None
    assert case['http'].post('/track/like', data={'video_id': 'owned-song', 'on': '1'}).status_code == 200
    assert case['store'].tracks.item_rating(case['main_id'], 'owned-song')['status'] == 'LIKE'
    assert provider.calls == ['rate_song'] * 4


@pytest.mark.parametrize('operation,boundary,kind,after', [
    ('save-album', 'get_album', 'json', False),
    ('save-album', 'rate_playlist', 'value', False),
    ('save-album', 'rate_playlist', 'json', True),
    ('unsave-album', 'get_album', 'value', False),
    ('unsave-album', 'rate_playlist', 'json', False),
    ('unsave-album', 'rate_playlist', 'value', True),
])
def test_album_provider_errors_keep_membership_and_do_not_replay(
        quiet_actions, kind, operation, boundary, after, caplog):
    case, provider = quiet_actions, quiet_actions['main']
    store = case['store']
    if operation == 'unsave-album':
        store.collection.add_saved_album({'browse': 'MPRE_new', 'title': 'New Album'}, account_id=case['main_id'])
        provider.saved.add('QUIET-AUDIO')
    before = store.collection.saved_album_owners('MPRE_new')
    provider.fault, provider.kind, provider.after = boundary, kind, after
    response = case['http'].post('/collection/' + operation,
        data={'browse_id': 'MPRE_new', 'identity_id': case['main_id']})
    assert response.status_code == 503 and response.content == b''
    assert response.headers['hx-reswap'] == 'none' and 'hx-refresh' not in response.headers
    assert DIAGNOSTIC in caplog.text
    assert store.collection.saved_album_owners('MPRE_new') == before
    calls = ['get_album'] + (['rate_playlist'] if boundary == 'rate_playlist' else [])
    assert provider.calls == calls
    assert provider.album_ratings == ([('QUIET-AUDIO', 'LIKE' if operation == 'save-album' else 'INDIFFERENT')]
                                      if boundary == 'rate_playlist' and after else [])
    assert case['http'].get('/albums').status_code == 200
    assert provider.calls == calls
    provider.fault = None
    response = case['http'].post('/collection/' + operation,
        data={'browse_id': 'MPRE_new', 'identity_id': case['main_id']})
    assert response.status_code == 200 and response.headers['hx-refresh'] == 'true'
    assert provider.calls == calls + ['get_album', 'rate_playlist']
    assert ('MPRE_new' in store.collection.saved_album_ids()) == (operation == 'save-album')
    assert ('QUIET-AUDIO' in provider.saved) == (operation == 'save-album')


@pytest.mark.parametrize('kind,after', [('value', False), ('json', True)])
def test_create_error_preserves_unknown_remote_outcome_without_replay(quiet_actions, kind, after, caplog):
    case, provider = quiet_actions, quiet_actions['main']
    before = case['store'].playlists.get_playlists()
    actions = case['store'].actions.get_actions()
    provider.fault, provider.kind, provider.after = 'create_playlist', kind, after
    response = case['http'].post('/songs/create-playlist', json={'name': 'Keep this draft', 'song': SONG})
    assert response.status_code == 502
    assert DIAGNOSTIC not in response.text and 'not confirmed' in response.json()['detail']
    assert DIAGNOSTIC in caplog.text
    assert case['store'].playlists.get_playlists() == before
    assert case['store'].actions.get_actions() == actions
    assert provider.calls == ['create_playlist']
    assert len(provider.created) == int(after) and not provider.added
    assert case['http'].get('/playlists').status_code == 200
    assert provider.calls == ['create_playlist'] and len(provider.created) == int(after)


@pytest.mark.parametrize('boundary', ['accounts', 'playlist'])
def test_storage_value_error_is_logged_instead_of_reclassified_as_validation(quiet_actions, monkeypatch, boundary, caplog):
    case = quiet_actions
    def fail(*args, **kwargs):
        raise ValueError(DIAGNOSTIC)
    if boundary == 'accounts':
        monkeypatch.setattr(case['store'].accounts, 'get_accounts', fail)
        response = case['http'].post('/track/like', data={'video_id': 'owned-song', 'on': '1'})
        assert response.status_code == 503 and response.content == b''
    else:
        monkeypatch.setattr(case['store'].playlists, 'get_playlist', fail)
        response = case['http'].post('/songs/add-to-playlist', json={
            'playlist_id': case['destination'], 'song': SONG})
        assert response.status_code == 502 and DIAGNOSTIC not in response.text
    assert DIAGNOSTIC in caplog.text
    assert not case['main'].calls and not case['other'].added


@pytest.mark.parametrize('boundary,kind,after', [
    ('get_album', 'json', False),
    ('create_playlist', 'value', False),
    ('create_playlist', 'json', True),
])
def test_album_creation_provider_error_keeps_unknown_work_and_never_replays(
        quiet_actions, kind, after, boundary, caplog):
    case, provider = quiet_actions, quiet_actions['main']
    before = case['store'].playlists.get_playlists()
    actions = case['store'].actions.get_actions()
    provider.fault, provider.kind, provider.after = boundary, kind, after
    response = case['http'].post('/album/create-playlist', data={
        'browse_id': 'MPRE_new', 'name': 'Retained album draft'})
    assert response.status_code == 503 and response.content == b''
    assert response.headers['hx-reswap'] == 'none'
    assert DIAGNOSTIC in caplog.text
    expected = ['get_album'] * 3 if boundary == 'get_album' else ['get_album', 'create_playlist']
    assert provider.calls == expected
    assert len(provider.created) == int(after and boundary == 'create_playlist')
    assert not provider.added
    assert case['store'].playlists.get_playlists() == before
    assert case['store'].actions.get_actions() == actions
    provider.fault = None
    assert case['http'].get('/album?browse=MPRE_new').status_code == 200
    assert provider.calls.count('create_playlist') == expected.count('create_playlist')
    assert not provider.added


def test_album_share_provider_error_keeps_diagnostic_in_logs(quiet_actions, caplog):
    caplog.set_level('INFO', logger='yt_playlist.web')
    case, provider = quiet_actions, quiet_actions['main']
    provider.fault, provider.kind = 'get_album', 'json'
    response = case['http'].get('/album/MPRE_new/share.txt')
    assert response.status_code == 503 and DIAGNOSTIC not in response.text
    assert 'full album could not be fetched' in response.json()['detail']
    assert DIAGNOSTIC in caplog.text
    assert provider.calls == ['get_album'] and not provider.created and not provider.album_ratings


def test_album_feedback_storage_error_is_quiet(quiet_actions, monkeypatch, caplog):
    from yt_playlist.rec import album_feedback
    case, provider = quiet_actions, quiet_actions['main']
    provider.kind = 'value'
    def fail(*args, **kwargs):
        provider.fail()
    monkeypatch.setattr(case['store'].album_feedback, 'replace', fail)
    response = case['http'].post('/album/feedback', data={'browse_id': 'MPRE_new', 'kind': 'more'})
    assert response.status_code == 503 and response.content == b''
    assert DIAGNOSTIC in caplog.text
    assert album_feedback.state(case['store'], ['MPRE_new']) == {}
    assert not provider.calls


@pytest.mark.parametrize('operation,kind', [
    ('add-tracks', 'json'), ('retry-placement', 'value'),
    ('rename', 'json'), ('remove-track', 'value'),
])
def test_playlist_mutation_lookup_failure_cannot_render_a_technical_toast(
        quiet_actions, monkeypatch, kind, operation, caplog):
    case, provider = quiet_actions, quiet_actions['main']
    store, pid = case['store'], case['source']
    before = store.charts.playlist_tracks_detail(pid)
    playlists = store.playlists.get_playlists()
    lookups = []

    def fail_lookup(playlist_id):
        lookups.append(playlist_id)
        if kind == 'json':
            raise json.JSONDecodeError(DIAGNOSTIC, '<html>unavailable</html>', 0)
        raise ValueError(DIAGNOSTIC)

    forms = {
        'add-tracks': {'track': json.dumps({'videoId': 'owned-live', 'title': 'Owned Song (Live)',
            'artists': [{'name': 'Artist & Friends'}], 'duration_seconds': 240}),
            'after_video_id': 'owned-song'},
        'retry-placement': {},
        'rename': {'title': 'Keep this playlist'},
        'remove-track': {'video_id': 'owned-song'},
    }
    with monkeypatch.context() as failing:
        failing.setattr(store.playlists, 'get_playlist', fail_lookup)
        response = case['http'].post(f'/playlist/{pid}/{operation}', data=forms[operation])

    assert lookups == [pid]
    assert response.status_code >= 400
    assert DIAGNOSTIC not in response.text
    assert 'toast-err' not in response.text
    assert DIAGNOSTIC in caplog.text
    assert 'hx-refresh' not in response.headers and 'hx-redirect' not in response.headers
    assert store.charts.playlist_tracks_detail(pid) == before
    assert store.playlists.get_playlists() == playlists
    assert not provider.created and not provider.added and not provider.deleted
    assert not provider.edited and not provider.rated
    assert case['http'].get(f'/playlist/{pid}').status_code == 200
    assert lookups == [pid]
    assert store.charts.playlist_tracks_detail(pid) == before
    assert not provider.created and not provider.added and not provider.deleted
    assert not provider.edited and not provider.rated


@pytest.mark.parametrize('operation,kind', [
    ('keep-one', 'json'), ('delete-empty', 'value'), ('forget-empty', 'json'),
])
def test_cleanup_lookup_failure_cannot_render_a_technical_toast(
        quiet_actions, monkeypatch, kind, operation, caplog):
    case, store = quiet_actions, quiet_actions['store']
    playlists = store.playlists.get_playlists()
    actions = store.actions.get_actions()
    tracks = {pid: store.charts.playlist_tracks_detail(pid)
              for pid in (case['source'], case['destination'])}
    pid = case['source'] if operation == 'keep-one' else case['destination']
    lookups = []

    def fail_lookup(playlist_id):
        lookups.append(playlist_id)
        if kind == 'json':
            raise json.JSONDecodeError(DIAGNOSTIC, '<html>unavailable</html>', 0)
        raise ValueError(DIAGNOSTIC)

    path = '/dupe/keep-one' if operation == 'keep-one' else '/playlist/' + operation
    form = {'keep' if operation == 'keep-one' else 'playlist': pid}
    with monkeypatch.context() as failing:
        failing.setattr(store.playlists, 'get_playlist', fail_lookup)
        response = case['http'].post(path, data=form)

    assert lookups == [pid]
    assert response.status_code >= 500
    assert DIAGNOSTIC not in response.text and 'toast-err' not in response.text
    assert response.headers['hx-reswap'] == 'none'
    assert 'hx-refresh' not in response.headers and 'hx-redirect' not in response.headers
    assert DIAGNOSTIC in caplog.text
    assert store.playlists.get_playlists() == playlists
    assert store.actions.get_actions() == actions
    assert {key: store.charts.playlist_tracks_detail(key) for key in tracks} == tracks
    assert case['http'].get('/cleanup').status_code == 200
    assert lookups == [pid]
    for provider in (case['main'], case['other']):
        assert not provider.created and not provider.added and not provider.deleted
        assert not provider.edited and not provider.rated


def test_merge_provider_parse_failure_cannot_render_a_technical_toast(
        quiet_actions, monkeypatch, caplog):
    from functools import partial
    from yt_playlist.library import executor
    from yt_playlist.util.retry import with_retry

    case, store, provider = quiet_actions, quiet_actions['store'], quiet_actions['main']
    ids = f"{case['source']},{case['destination']}"
    url = '/merge?ids=' + ids
    assert case['http'].get(url).status_code == 200
    assert case['http'].post('/merge/update?ids=' + ids,
                             data={'field': 'setall', 'value': '0'}).status_code == 200
    draft = store.actions.get_draft(ids)
    playlists = store.playlists.get_playlists()
    actions = store.actions.get_actions()
    tracks = {pid: store.charts.playlist_tracks_detail(pid)
              for pid in (case['source'], case['destination'])}
    reads = []

    def fail_read(playlist_id, limit=100):
        reads.append(playlist_id)
        raise json.JSONDecodeError(DIAGNOSTIC, '<html>unavailable</html>', 0)

    with monkeypatch.context() as failing:
        failing.setattr(provider, 'get_playlist', fail_read)
        # Keep the real retry policy and its three reads; skip only backoff sleep.
        failing.setattr(executor, 'with_retry', partial(with_retry, sleep=lambda delay: None))
        response = case['http'].post('/merge/apply?ids=' + ids)

    assert reads == ['SOURCE'] * 3
    assert response.status_code >= 500
    assert DIAGNOSTIC not in response.text and 'toast-err' not in response.text
    assert response.headers['hx-reswap'] == 'none'
    assert 'hx-refresh' not in response.headers and 'hx-redirect' not in response.headers
    assert DIAGNOSTIC in caplog.text
    assert store.actions.get_draft(ids) == draft
    assert store.actions.get_actions() == actions
    assert store.playlists.get_playlists() == playlists
    assert {pid: store.charts.playlist_tracks_detail(pid) for pid in tracks} == tracks
    assert case['http'].get(url).status_code == 200
    refreshed = store.actions.get_draft(ids)
    assert refreshed.id == draft.id and refreshed.params_json == draft.params_json
    assert reads == ['SOURCE'] * 3
    for client in (provider, case['other']):
        assert not client.created and not client.added and not client.deleted
        assert not client.edited and not client.rated
