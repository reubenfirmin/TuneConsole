"""Missing remote playlists and explicit local removal never authorize a remote deletion."""
import json

import pytest
from fastapi.testclient import TestClient
from ytmusicapi.exceptions import YTMusicServerError

from tests.conftest import FakeClient
from yt_playlist.library import executor
from yt_playlist.rec import actions
from yt_playlist.rec.actions import CLEANUP_SURFACE
from yt_playlist.util.action_kinds import REMOVE_LOCAL_PLAYLIST, is_undoable
from yt_playlist.web.app import create_app


@pytest.fixture
def missing(store, monkeypatch, tmp_path):
    monkeypatch.setenv('YT_PLAYLIST_HOME', str(tmp_path))
    monkeypatch.setattr(executor, 'with_retry', lambda fn: fn())
    iid = store.identities.upsert_identity('Main', 'cred', None, True)
    pid = store.playlists.upsert_playlist(iid, 'MISSING', 'Gone from YouTube', 0, '', 1)
    client = FakeClient()
    app = create_app(store, lambda: {iid: client}, now_fn=lambda: 2)
    http = TestClient(app, base_url='http://127.0.0.1')
    actions.refresh_cleanup(store, 1)
    return pid, client, http


def test_production_http_404_removes_only_the_empty_local_entry(missing, store, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from yt_playlist.core.identities import build_client

    class Bridge:
        def execute(self, *args):
            return 404, json.dumps({'error': {'code': 404, 'message': 'Not found'}})

    pid, client, http = missing
    reader = build_client(SimpleNamespace(brand_account_id=None), Bridge())
    monkeypatch.setattr(client, 'get_playlist', reader.get_playlist)
    assert http.post('/playlist/delete-empty', data={'playlist': pid}).status_code == 200
    assert store.playlists.get_playlist(pid) is None
    assert not client.deleted and not list(tmp_path.glob('backups/*'))
    assert store.rec_surface.get_proposals(CLEANUP_SURFACE)['count'] == 0
    action, = store.actions.get_actions()
    assert action.kind == REMOVE_LOCAL_PLAYLIST and not is_undoable(action.kind)
    assert json.loads(action.params_json)['reason'] == 'remote_missing'
    log = http.get('/actions').text
    assert 'YouTube unchanged' in log and f'action="/undo/{action.id}"' not in log


@pytest.mark.parametrize('failure', [
    TimeoutError('timeout'), KeyError('musicPlaylistShelfRenderer'),
    YTMusicServerError('Server returned HTTP 401: Unauthorized.'),
    YTMusicServerError('Server returned HTTP 403: Forbidden.'),
    YTMusicServerError('Server returned HTTP 500: Unavailable.'),
    ValueError('An unrelated error mentions Server returned HTTP 404: Not found'),
])
def test_unreadable_remote_requires_explicit_local_removal(missing, store, monkeypatch, failure):
    pid, client, http = missing

    def unreadable(*args, **kwargs):
        raise failure

    monkeypatch.setattr(client, 'get_playlist', unreadable)
    assert http.post('/playlist/delete-empty', data={'playlist': pid}).status_code == 422
    assert store.playlists.get_playlist(pid) is not None and not store.actions.get_actions()
    assert http.post('/playlist/forget-empty', data={'playlist': pid}).status_code == 200
    assert store.playlists.get_playlist(pid) is None and not client.deleted
    action, = store.actions.get_actions()
    assert json.loads(action.params_json)['reason'] == 'user_requested'


@pytest.mark.parametrize('count,has_member', [(1, False), (0, True)])
@pytest.mark.parametrize('endpoint', ['delete-empty', 'forget-empty'])
def test_local_removal_preserves_cached_content(missing, store, monkeypatch, count, has_member, endpoint):
    pid, client, http = missing
    store.playlists.set_playlist_track_count(pid, count, 2)
    if has_member:
        tid = store.tracks.upsert_track('cached', 'Cached song', 'Artist', None, 200)
        store.playlists.set_playlist_tracks(pid, [tid])

    def absent(*args, **kwargs):
        raise YTMusicServerError('Server returned HTTP 404: Not Found.')

    monkeypatch.setattr(client, 'get_playlist', absent)
    assert http.post('/playlist/' + endpoint, data={'playlist': pid}).status_code == 422
    assert store.playlists.get_playlist(pid) is not None and not client.deleted
    assert not store.actions.get_actions()


def test_explicit_removal_cannot_drop_system_playlist(missing, store):
    _, client, http = missing
    iid = store.identities.get_identities()[0].id
    system = store.playlists.upsert_playlist(iid, 'LM', 'Liked Music', 0, '', 1)
    assert http.post('/playlist/forget-empty', data={'playlist': system}).status_code == 422
    assert store.playlists.get_playlist(system) is not None and not client.deleted


def test_disappearing_after_empty_read_keeps_the_verified_backup(missing, store, monkeypatch, tmp_path):
    pid, client, http = missing

    def already_deleted(*args, **kwargs):
        raise YTMusicServerError('Server returned HTTP 404: Not Found.')

    monkeypatch.setattr(client, 'delete_playlist', already_deleted)
    assert http.post('/playlist/delete-empty', data={'playlist': pid}).status_code == 200
    assert store.playlists.get_playlist(pid) is None
    backup, = tmp_path.glob('backups/*.json')
    assert json.loads(backup.read_text())['tracks'] == []
