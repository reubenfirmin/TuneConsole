"""Real action publication covers interruption, restart and concurrent revisions."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from tests.store_fixture_support import file_consumer_store, native_sql_calls, without_query_timing
from yt_playlist.core.collection_files import record_path
from yt_playlist.core import action_backup_files, fao
from yt_playlist.core.store import Store

pytestmark = pytest.mark.usefixtures('native_sql_calls', 'without_query_timing')


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These persistence checks do not construct the web application."""


@pytest.mark.parametrize('failure', ['file', 'status'])
def test_interrupted_action_and_step_leave_only_pending_records_until_retry(
        file_consumer_store, monkeypatch, failure):
    store = file_consumer_store
    account = store.accounts.upsert_account('Owner', 'owner.json', None, True)
    membership = store.playlists.upsert_playlist(account, 'REMOTE', 'Playlist', 0, '', 1)
    plan = json.dumps(dict(playlist_id=membership, video_ids=['one', 'one']))
    files, repo = store.actions.files, store.actions.repository
    publish, complete = files.publish_reserved, repo._complete_file

    def write(identity, kind, owner, value, **kwargs):
        assert not store.conn.in_transaction
        with closing(sqlite3.connect(store._db_path)) as observer:
            assert observer.execute('SELECT kind,owner_id,file_status FROM collection_file_writes WHERE id=?',
                                    (identity,)).fetchone() == (kind, str(owner), 'pending')
        if failure == 'file' and kind == 'action-step':
            raise OSError('action interruption')
        return publish(identity, kind, owner, value, **kwargs)

    def commit(kind, owner, publication):
        reference = complete(kind, owner, publication)
        if failure == 'status' and kind == 'action-step':
            raise OSError('action interruption')
        return reference

    with monkeypatch.context() as patch:
        patch.setattr(files, 'publish_reserved', write)
        patch.setattr(repo, '_complete_file', commit)
        with pytest.raises(OSError, match='action interruption'):
            store.actions.record_action('add_tracks', '{}', plan, 'pending', '{}', 1, idempotency_key='intent')
    assert store.actions.get_actions() == []
    assert not store.conn.in_transaction
    rows = store.conn.execute('SELECT * FROM collection_file_writes ORDER BY id').fetchall()
    assert [row['kind'] for row in rows] == ['action', 'action-step']
    assert all(row['file_status'] == 'pending' for row in rows)
    for row in rows:
        assert record_path(files.root, row['id']).exists() is (failure == 'status' or row['kind'] == 'action')

    restarted = Store(store._db_path)
    try:
        restarted.init_schema(defer_metadata=True)
        assert restarted.actions.get_actions() == []
        identity = restarted.actions.record_action('add_tracks', '{}', plan, 'pending', '{}', 2, idempotency_key='intent')
        assert identity > rows[-1]['id']
        assert json.loads(restarted.actions.get_action(identity).plan_json)['video_ids'] == ['one', 'one']
        for row in restarted.conn.execute("SELECT * FROM collection_file_writes WHERE file_status='ready'"):
            assert Path(record_path(files.root, row['id'])).is_file()
        assert not restarted.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        restarted.conn.close()


def test_concurrent_update_during_action_file_write_cannot_replace_the_winning_revision(
        file_consumer_store, monkeypatch):
    store = file_consumer_store
    identity = store.actions.record_action('remove_track', '{}', '{}', 'executed', '{}', 1)
    publish = store.actions.files.publish_reserved
    winner = Store(store._db_path)
    try:
        winner.init_schema(defer_metadata=True)
        def race(*args, **kwargs):
            assert not store.conn.in_transaction
            reference = publish(*args, **kwargs)
            winner.actions.update_action(identity, 'executed', 3, undo_json='{"marker":"winner"}')
            return reference

        with monkeypatch.context() as patch:
            patch.setattr(store.actions.files, 'publish_reserved', race)
            with pytest.raises(ValueError, match='updated by another worker'):
                store.actions.update_action(identity, 'executed', 2, undo_json='{"marker":"stale"}')
        assert json.loads(store.actions.get_action(identity).undo_json) == {'marker': 'winner'}
        assert [row[0] for row in store.conn.execute('SELECT file_status FROM collection_file_writes ORDER BY id')] == [
            'ready', 'pending', 'ready']
        assert not store.conn.in_transaction
    finally:
        winner.conn.close()


@pytest.mark.parametrize('failure', ['file', 'status'])
def test_interrupted_backup_copy_keeps_the_prior_action_and_can_retry_after_restart(
        file_consumer_store, tmp_path, monkeypatch, failure):
    store = file_consumer_store
    identity = store.actions.record_action('remove_track', '{}', '{}', 'executed', '{}', 1)
    source = tmp_path / 'playlist.json'
    encoded = b'{"snapshot_version":2,"ytm_playlist_id":"REMOTE","tracks":[]}'
    fao.write(source, encoded)
    undo = json.dumps({'backups': [str(source)]})
    target, method = ((store.actions.backups, 'retain_reserved') if failure == 'file'
                      else (store.actions.repository, 'store_backup'))

    def interrupted(*args, **kwargs):
        raise OSError('backup publication interrupted')

    with monkeypatch.context() as patch:
        patch.setattr(target, method, interrupted)
        with pytest.raises(OSError, match='backup publication interrupted'):
            store.actions.update_action(identity, 'executed', 2, undo_json=undo)
    assert json.loads(store.actions.get_action(identity).undo_json) == {}
    assert not store.conn.in_transaction
    with closing(sqlite3.connect(store._db_path)) as observer:
        row = observer.execute('SELECT id,action_id,position,file_status FROM action_backup_writes').fetchone()
    assert row[1:] == (identity, 0, 'pending')
    path = action_backup_files.record_path(store.actions.backups.root, row[0])
    assert path.exists() is (failure == 'status')
    assert not store.conn.execute('SELECT 1 FROM action_backups').fetchone()

    restarted = Store(store._db_path)
    try:
        restarted.init_schema(defer_metadata=True)
        assert json.loads(restarted.actions.get_action(identity).undo_json) == {}
        restarted.actions.update_action(identity, 'executed', 3, undo_json=undo)
        source.unlink()
        row = restarted.conn.execute('SELECT * FROM action_backups').fetchone()
        retained = row['retained_filename']
        assert action_backup_files.record_id(retained) is not None
        assert fao.read(retained).bytes == encoded
        assert json.loads(restarted.actions.get_action(identity).undo_json)['backups'] == [retained]
        assert [row[0] for row in restarted.conn.execute(
            'SELECT file_status FROM action_backup_writes ORDER BY id')] == ['pending', 'ready']
        assert not restarted.conn.execute('PRAGMA foreign_key_check').fetchone()
    finally:
        restarted.conn.close()
