"""Current backup custody survives a seek key moving behind a paused scan."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_action_work_migration import backup
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema,
    native_sql_calls, without_query_timing,
)
from yt_playlist.core import action_backup_files, collection_recovery as recovery
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.repos import collection_retention
from yt_playlist.controllers.collection_retention import CollectionCollector


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These collector checks do not construct an HTTP application."""


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('rock', 'Rock'), ('jazz', 'Jazz'))) as source:
        yield source


@pytest.fixture
def file_backed_store(tmp_path, _pristine_current_schema):
    with _file_consumer_store(tmp_path, _pristine_current_schema) as store:
        yield store


@pytest.mark.parametrize('move', ['unchanged', 'position', 'action_id'])
def test_retained_backup_survives_seek_key_move(
        file_backed_store, tmp_path, monkeypatch, native_sql_calls, without_query_timing, move):
    store = file_backed_store
    try:
        earlier = store.actions.record_action('copy_playlist', '{}', '{}', 'executed', '{}', 1)
        account = store.accounts.upsert_account('Owner', 'owner.json', None, True)
        deletions = []
        for index in range(3):
            remote = f'BACKUP-{index}'
            store.playlists.upsert_playlist(account, remote, remote, 2, '', 1)
            deletions.append({'identity': account, 'ytm': remote, 'phase': 'pending',
                              'backup': backup(tmp_path, account, remote)})
        action = store.actions.record_action('delete_playlist', '{}', '{}', 'delete_pending',
                                             json.dumps({'deletions': deletions}), 2)
        with store.conn:
            store.conn.execute('UPDATE action_backups SET position=position+10 WHERE action_id=?', (action,))
        owner = store.conn.execute('SELECT retained_filename,digest,bytes FROM action_backups '
                                   'WHERE action_id=? AND position=12', (action,)).fetchone()
        target = Path(owner['retained_filename'])
        exact = action_backup_files.verified(target, owner['digest'], owner['bytes'])[0]
        collector = CollectionCollector(recovery.directory(store.conn))
        collector.files.batch, collector.files.complete = [target], True
        store.retention.database._collection_orphans = collector
        monkeypatch.setattr(collector, 'OWNER_PAGE', 1)
        check = collection_retention.check_cancellable_work

        def pause_after_first_backup():
            if (collector.owners._table_index < len(collector.owners._tables)
                    and collector.owners._tables[collector.owners._table_index][0] == 'action_backups'
                    and collector.owners._after is not None):
                raise WorkBudgetExceeded('Pause after the first retained backup')
            check()

        with monkeypatch.context() as pause:
            pause.setattr(collection_retention, 'check_cancellable_work', pause_after_first_backup)
            for _ in range(50):
                store.retention.collections.maintain()
                if collector.owners._after == (action, 10):
                    break
            else:
                pytest.fail('Did not pause the partial backup ownership scan')
        with closing(sqlite3.connect(store._db_path)) as writer:
            writer.execute('PRAGMA foreign_keys=ON')
            with writer:
                if move == 'position':
                    writer.execute('UPDATE action_backups SET position=0 WHERE action_id=? AND position=12', (action,))
                elif move == 'action_id':
                    writer.execute('UPDATE action_backups SET action_id=? WHERE action_id=? AND position=12',
                                   (earlier, action))
        assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
        assert store.conn.execute('SELECT 1 FROM action_backups WHERE retained_filename=?', (str(target),)).fetchone()
        for _ in range(100):
            store.retention.collections.maintain()
            if not store.retention.collections.pending():
                break
        assert not store.retention.collections.pending()
        assert target.exists(), 'A currently retained backup must survive a committed seek-key move'
        assert action_backup_files.verified(target, owner['digest'], owner['bytes'])[0] == exact
    finally:
        store.retention.collections.close()
