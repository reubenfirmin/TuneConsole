"""Current backup custody survives a seek key moving behind a paused scan."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pytest

from tests.core.test_action_work_migration import backup
from tests.genre_catalog_support import bound_catalog
from tests.store_fixture_support import (
    _file_consumer_store, bounded_consumer_schema, install_numbered_input,
    native_sql_calls, without_query_timing,
)
from yt_playlist.core import action_backup_files, collection_recovery as recovery, schema_inspection
from yt_playlist.core.activity import WorkBudgetExceeded
from yt_playlist.core.migrations import regular
from yt_playlist.core.store import Store


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These collector checks do not construct an HTTP application."""


@pytest.fixture(scope='module')
def before_key_guards():
    # Recovery owns the interrupted scan, not replay of the preceding empty
    # schema. Preserve the exact pre-key-trigger SQL and regular history.
    with closing(sqlite3.connect(':memory:')) as source, pytest.MonkeyPatch.context() as patch:
        bound_catalog(patch)
        install_numbered_input(source, 107, regular_owners=regular.CHANGES[:27])
        source.execute('PRAGMA query_only=ON')
        assert source.execute('SELECT max(version) FROM schema_migrations').fetchone()[0] == 107
        assert regular.validate(source) == 27
        assert source.execute('PRAGMA query_only').fetchone()[0] == 1
        assert [row[1] for row in source.execute('PRAGMA table_info(action_backups)') if row[5]] == [
            'action_id', 'position']
        assert source.execute("SELECT 1 FROM sqlite_schema WHERE name='collection_recovery_epoch'").fetchone()
        assert not source.execute(
            "SELECT 1 FROM sqlite_schema WHERE name='collection_recovery_changed_action_backups_key'").fetchone()
        yield source


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema((('rock', 'Rock'), ('jazz', 'Jazz'))) as source:
        yield source


@pytest.fixture
def file_backed_store(tmp_path, _pristine_current_schema):
    with _file_consumer_store(tmp_path, _pristine_current_schema) as store:
        yield store


@pytest.mark.parametrize('key_guards', [False, True], ids=['pending-ddl', 'current'])
@pytest.mark.parametrize('move', ['unchanged', 'position', 'action_id'])
def test_retained_backup_survives_seek_key_move(
        request, tmp_path, monkeypatch, native_sql_calls, without_query_timing, key_guards, move):
    if key_guards:
        store = request.getfixturevalue('file_backed_store')
    else:
        store = Store(tmp_path / 'before-key-guards.db')
        request.addfinalizer(store.conn.close)
        store.conn.execute('PRAGMA synchronous=OFF')
        request.getfixturevalue('before_key_guards').backup(store.conn)
        with schema_inspection.reuse_tables(store.conn):
            store.init_schema(defer_metadata=True)
        assert regular.validate(store.conn) == 27
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
        collector = recovery.OrphanCollector(recovery.directory(store.conn))
        collector.batch, collector.complete = [target], True
        store._collection_orphans = collector
        monkeypatch.setattr(collector, 'OWNER_PAGE', 1)
        check = recovery.check_cancellable_work

        def pause_after_first_backup():
            if (collector._table_index < len(collector._tables)
                    and collector._tables[collector._table_index][0] == 'action_backups'
                    and collector._after is not None):
                raise WorkBudgetExceeded('Pause after the first retained backup')
            check()

        with monkeypatch.context() as pause:
            pause.setattr(recovery, 'check_cancellable_work', pause_after_first_backup)
            for _ in range(50):
                recovery.maintain(store)
                if collector._after == (action, 10):
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
            recovery.maintain(store)
            if not recovery.pending(store):
                break
        assert not recovery.pending(store)
        assert target.exists(), 'A currently retained backup must survive a committed seek-key move'
        assert action_backup_files.verified(target, owner['digest'], owner['bytes'])[0] == exact
    finally:
        recovery.close(store)
        if not key_guards:
            store.conn.close()
