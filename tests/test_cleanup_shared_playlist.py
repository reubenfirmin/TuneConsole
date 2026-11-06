"""Saved account memberships must never become copies to delete."""
import json
from pathlib import Path

import pytest

from tests.conftest import FakeClient, _track
from yt_playlist.library import analysis
from yt_playlist.library.executor import (
    MergePlan, _verify_and_delete, execute_planned, serialize_plan)
from yt_playlist.library.ops import PlaylistOps
from yt_playlist.library.playlist_undo import undo_action


@pytest.fixture
def shared_playlist(store):
    owner = store.accounts.upsert_account('owner', 'bridge', None, True)
    saved_by = store.accounts.upsert_account('saved', 'bridge', None, False)
    track = store.tracks.upsert_track('one', 'First song', 'Artist', None, 200)
    memberships = []
    for account in (owner, saved_by):
        playlist = store.playlists.upsert_playlist(account, 'SHARED', 'Shared', 1, '', 1)
        store.playlists.set_playlist_tracks(playlist, [track])
        memberships.append(playlist)
    clients = {account: FakeClient(tracks={'SHARED': [_track('one', 'First song', 'Artist')]})
               for account in (owner, saved_by)}
    ops = PlaylistOps(store, lambda: clients, lambda: 2)
    return memberships, clients, ops, track


def test_cleanup_compares_remote_lists_once_and_preserves_memberships(store, shared_playlist):
    memberships, clients, ops, track = shared_playlist
    state = analysis.prepare_cleanup(store)
    assert len(state.memberships.playlists) == 2
    assert len(state.playlists) == 1
    assert not analysis.find_identical_groups(state)
    assert state.relationships(0) == ([], [])

    account = next(iter(clients))
    duplicate = store.playlists.upsert_playlist(account, 'COPY', 'Copy', 1, '', 1)
    store.playlists.set_playlist_tracks(duplicate, [track])
    state = analysis.prepare_cleanup(store)
    assert len(state.memberships.playlists) == 3
    groups = analysis.find_identical_groups(state)
    assert len(groups) == 1
    assert {p.provider_playlist_id for p in groups[0].playlists} == {'SHARED', 'COPY'}
    assert len(state.relationships(0)[0]) == 1

    other = store.tracks.upsert_track('two', 'Second song', 'Artist', None, 200)
    store.playlists.set_playlist_tracks(duplicate, [track, other])
    state = analysis.prepare_cleanup(store)
    assert len(state.relationships(1)[1]) == 1
    assert len(store.playlists.get_playlists()) == 3


@pytest.mark.parametrize('keeper', [0, 1])
def test_keep_one_preserves_both_memberships_of_keeper(store, shared_playlist, keeper):
    memberships, clients, ops, track = shared_playlist
    assert ops.keep_one(memberships[keeper]) == (0, [])
    assert all(not client.deleted for client in clients.values())
    assert all(store.playlists.get_playlist(pid) is not None for pid in memberships)
    assert not store.actions.get_actions()


@pytest.mark.parametrize('keeper', [0, 1])
@pytest.mark.parametrize('entry', ['delete', 'stored', 'merge', 'keep_all', 'deletion_gate'])
def test_alias_requests_are_refused_before_remote_work(store, shared_playlist, monkeypatch, keeper, entry):
    memberships, clients, ops, track = shared_playlist
    source, target = memberships[1 - keeper], memberships[keeper]
    for client in clients.values():
        monkeypatch.setattr(client, 'get_playlist', lambda *a, **k: pytest.fail('alias reached remote work'))
    if entry == 'stored':
        # Reproduce an already stored plan from before this guard existed.
        params, payload = serialize_plan(MergePlan(source, target, [], []), 'delete', 'SHARED')
        action = store.actions.record_action('plan', params, payload, 'planned', '{}', 1)
        run = lambda: execute_planned(store, action, clients, 2)
    elif entry == 'delete':
        run = lambda: ops.delete_dupe(source, target)
    elif entry == 'deletion_gate':
        source_client = clients[store.playlists.get_playlist(source).account_id]
        target_client = clients[store.playlists.get_playlist(target).account_id]
        run = lambda: _verify_and_delete(store, source, source_client, target_client,
                                         'SHARED', 2, action_id=1)
    else:
        run = lambda: ops.apply_merge(memberships, ['one'], 'all' if entry == 'keep_all' else target)
    with pytest.raises(ValueError, match='same YouTube Music playlist|kept YouTube Music playlist'):
        run()
    assert all(not client.deleted and not client.added and not client.removed for client in clients.values())
    assert all(store.playlists.get_playlist(pid) is not None for pid in memberships)


@pytest.mark.parametrize('entry', ['delete', 'keep_one', 'merge'])
def test_distinct_cross_account_copy_can_be_deleted_and_undone(
        store, shared_playlist, tmp_path, monkeypatch, entry):
    monkeypatch.setenv('YT_PLAYLIST_HOME', str(tmp_path))
    memberships, clients, ops, track = shared_playlist
    owner, saved_by = list(clients)
    duplicate = store.playlists.upsert_playlist(saved_by, 'COPY', 'Copy', 1, '', 1)
    store.playlists.set_playlist_tracks(duplicate, [track])
    clients[saved_by]._tracks['COPY'] = [_track('one', 'First song', 'Artist')]
    if entry == 'delete':
        ops.delete_dupe(duplicate, memberships[0])
    elif entry == 'keep_one':
        assert ops.keep_one(memberships[0]) == (1, [])
    else:
        result = ops.apply_merge([memberships[0], duplicate], ['one'], memberships[0])
        assert result['deleted'] == ['Copy']
    assert clients[saved_by].deleted == ['COPY']
    assert not clients[owner].deleted
    assert all(store.playlists.get_playlist(pid) is not None for pid in memberships)
    action = store.actions.get_actions()[0]
    assert action.status == 'executed'
    undo = json.loads(action.undo_json)
    assert len(undo['deletions']) == 1
    deletion = undo['deletions'][0]
    assert deletion['phase'] == 'complete'
    assert deletion['ytm'] == 'COPY' and deletion['identity'] == saved_by
    assert Path(deletion['backup']).exists()
    undo_action(store, action.id, clients, 3)
    assert store.actions.get_action(action.id).status == 'undone'
    assert clients[saved_by].created
    assert all(store.playlists.get_playlist(pid) is not None for pid in memberships)


@pytest.mark.parametrize('refusal', ['forbidden', 'all_forbidden', 'uncertain', 'server_error'])
def test_keep_one_retains_owner_alternative_but_never_retries_uncertain_deletion(
        store, tmp_path, monkeypatch, refusal):
    from ytmusicapi.exceptions import YTMusicServerError

    monkeypatch.setenv('YT_PLAYLIST_HOME', str(tmp_path))
    accounts = [store.accounts.upsert_account(name, 'bridge', None, index == 0)
                for index, name in enumerate(('Keeper', 'Saved copy', 'Copy owner'))]
    track = store.tracks.upsert_track('one', 'First song', 'Artist', None, 200)
    playlists, clients = [], {}
    for account, remote in zip(accounts, ('KEEP', 'COPY', 'COPY')):
        pid = store.playlists.upsert_playlist(account, remote, remote, 1, '', 1)
        store.playlists.set_playlist_tracks(pid, [track])
        playlists.append(pid)
        clients[account] = FakeClient(tracks={remote: [_track('one', 'First song', 'Artist')]})
    kept_alias = store.playlists.upsert_playlist(accounts[1], 'KEEP', 'Saved keeper', 1, '', 1)
    store.playlists.set_playlist_tracks(kept_alias, [track])
    attempts = []

    def reject(remote):
        attempts.append((accounts[1], remote))
        if refusal in ('forbidden', 'all_forbidden'):
            raise YTMusicServerError('Server returned HTTP 403: Forbidden.')
        if refusal == 'server_error':
            raise YTMusicServerError('Server returned HTTP 503: Unavailable.')
        # The destructive operation may already have happened before timeout.
        clients[accounts[1]].deleted.append(remote)
        raise TimeoutError('Lost deletion receipt')

    monkeypatch.setattr(clients[accounts[1]], 'delete_playlist', reject)
    if refusal == 'all_forbidden':
        def refuse_owner(remote):
            attempts.append((accounts[2], remote))
            raise YTMusicServerError('Server returned HTTP 403: Forbidden.')
        monkeypatch.setattr(clients[accounts[2]], 'delete_playlist', refuse_owner)
    ops = PlaylistOps(store, lambda: clients, lambda: 2)
    deleted, errors = ops.keep_one(playlists[0])
    assert not clients[accounts[0]].deleted
    assert store.playlists.get_playlist(playlists[0]) is not None
    assert store.playlists.get_playlist(kept_alias) is not None
    actions = store.actions.get_actions()
    for action in actions:
        entry, = json.loads(action.undo_json)['deletions']
        assert Path(entry['backup']).exists()
    if refusal == 'forbidden':
        assert (deleted, errors) == (1, [])
        assert clients[accounts[2]].deleted == ['COPY']
        assert all(store.playlists.get_playlist(pid) is None for pid in playlists[1:])
        assert not analysis.find_identical_groups(analysis.prepare_cleanup(store))
        assert {a.status for a in actions} == {'delete_failed', 'executed'}
        owner_action = next(a for a in actions if a.status == 'executed')
        undo_action(store, owner_action.id, clients, 3)
        assert clients[accounts[2]].created
        assert store.actions.get_action(owner_action.id).status == 'undone'
    elif refusal == 'all_forbidden':
        assert deleted == 0 and len(errors) == 1 and 'permission' in errors[0]
        assert len(attempts) == 2 and all(a.status == 'delete_failed' for a in actions)
        assert all(store.playlists.get_playlist(pid) is not None for pid in playlists[1:])
    else:
        assert deleted == 0 and len(errors) == 1
        assert all(store.playlists.get_playlist(pid) is not None for pid in playlists[1:])
        assert not clients[accounts[2]].deleted and len(actions) == 1
        assert actions[0].status == 'delete_pending'
        assert json.loads(actions[0].undo_json)['deletions'][0]['phase'] == 'uncertain'
        # Reordering memberships on a later invocation cannot bypass the saved
        # uncertain attempt through the other account either.
        original = store.playlists.get_playlists
        monkeypatch.setattr(store.playlists, 'get_playlists', lambda: list(reversed(original())))
        assert ops.keep_one(playlists[0])[0] == 0
        assert len(attempts) == 1 and not clients[accounts[2]].deleted


@pytest.mark.parametrize('remote', ['LM', 'SE'])
def test_confirmed_system_list_absence_only_prunes_its_own_account(store, remote):
    from types import SimpleNamespace
    from yt_playlist.library.playlist_deletion import _prune

    accounts = [store.accounts.upsert_account(name, 'bridge', None, index == 0)
                for index, name in enumerate(('One', 'Two'))]
    memberships = [store.playlists.upsert_playlist(account, remote, remote, 0, '', 1)
                   for account in accounts]
    _prune(store, SimpleNamespace(kind='plan'), {'identity': accounts[0], 'ytm': remote})
    assert store.playlists.get_playlist(memberships[0]) is None
    assert store.playlists.get_playlist(memberships[1]) is not None
