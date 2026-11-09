"""A failed guard refresh must leave a live Store usable for immediate retries."""
from contextlib import closing, nullcontext
import sqlite3
from types import SimpleNamespace

import pytest

from yt_playlist.core import activity
from yt_playlist.core.db_revisions import Revisions


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """These real SQL readers do not construct an HTTP app or its workers."""


@pytest.mark.parametrize('database', ['store', 'file_backed_store'])
@pytest.mark.parametrize('entrypoint', ['snapshot', 'tracked-view'])
@pytest.mark.parametrize('failure', ['cancel', 'timeout', 'schema-denied', 'trigger-denied'])
def test_failed_revision_refresh_retries_without_writes_or_restart(
        request, monkeypatch, database, entrypoint, failure):
    store = request.getfixturevalue(database)
    store.settings.set_setting('guard-input', 'committed')
    clock = [0.0]
    if failure == 'timeout':
        monkeypatch.setattr(activity, 'time', SimpleNamespace(monotonic=lambda: clock[0]))

    with store.read_snapshot() as older:
        assert older.store.settings.get_setting('guard-input') == 'committed'
        if store._db_path is None:
            store.conn.execute('CREATE TABLE guard_new_owner(value TEXT)')
        else:
            with closing(sqlite3.connect(store._db_path)) as external, external:
                external.execute('CREATE TABLE guard_new_owner(value TEXT)')
        assert not store.conn.in_transaction

        state = {'schema_reads': 0, 'armed': False, 'statement': '',
                 'interrupted_at': '', 'triggers': 0}

        def trace(sql):
            state['statement'] = sql
            if sql == 'PRAGMA main.schema_version':
                state['schema_reads'] += 1
                state['armed'] = state['schema_reads'] == 2
                if state['armed'] and failure == 'timeout':
                    clock[0] = 2.0

        def stop():
            if state['armed']:
                if not state['interrupted_at']:
                    state['interrupted_at'] = state['statement']
                return True
            return False

        def authorize(operation, table, column, database, trigger):
            if state['armed'] and failure == 'schema-denied' and operation == sqlite3.SQLITE_READ:
                if table in ('sqlite_master', 'sqlite_schema'):
                    return sqlite3.SQLITE_DENY
            if operation == sqlite3.SQLITE_CREATE_TEMP_TRIGGER:
                state['triggers'] += 1
                if failure == 'trigger-denied' and state['triggers'] == 2:
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        if failure == 'cancel':
            scope = activity.cancellable_work(stop, stop_on_shutdown=False)
            error = activity.WorkClosing
        elif failure == 'timeout':
            scope = activity.work_budget(1)
            error = activity.WorkBudgetExceeded
        else:
            scope = nullcontext()
            error = sqlite3.DatabaseError

        store.conn.set_trace_callback(trace)
        store.conn.set_authorizer(authorize)
        try:
            with pytest.raises(error), scope:
                reader = (store.read_snapshot() if entrypoint == 'snapshot' else
                          store.read_view(tracked=True, seconds=None))
                with reader:
                    pytest.fail('Failed guard construction yielded a reader')
        finally:
            store.conn.set_trace_callback(None)
            store.conn.set_authorizer(None)
        if failure == 'cancel':
            assert "SELECT name FROM main.sqlite_schema WHERE type='view'" == state['interrupted_at']
        if failure == 'trigger-denied':
            assert state['triggers'] == 2
        assert not store.conn.in_transaction
        assert not store.conn.execute(
            "SELECT 1 FROM sqlite_temp_schema WHERE name='snapshot_revisions'").fetchone()

        # Neither retry may require an unrelated write to release a transaction.
        for _ in range(2):
            with store.read_snapshot() as retry:
                assert retry.store.settings.get_setting('guard-input') == 'committed'
                assert 'guard_new_owner' in store._snapshot_revisions.tables
                guards = {row[0] for row in store.conn.execute(
                    "SELECT name FROM sqlite_temp_schema WHERE type='trigger'")}
                assert {f'snapshot_revision_{table}_{operation}'
                        for table in store._snapshot_revisions.tables
                        for operation in ('insert', 'update', 'delete')} <= guards
                assert retry.publish(lambda: None)
        assert not older.publish(lambda: pytest.fail('Published across an external schema epoch'))

    with store.read_snapshot() as retry:
        assert retry.store.settings.get_setting('guard-input') == 'committed'
        assert not retry.store.conn.execute('SELECT * FROM guard_new_owner').fetchall()
        with store.conn:
            store.conn.execute("INSERT INTO guard_new_owner VALUES ('continuing write')")
        assert not retry.publish(lambda: pytest.fail('Published changed inputs after retry'))
    with store.read_snapshot() as current:
        assert current.store.conn.execute('SELECT value FROM guard_new_owner').fetchone()[0] == 'continuing write'
        assert current.publish(lambda: None)


@pytest.mark.parametrize('fail', [False, True])
def test_guard_construction_keeps_application_transaction_ownership(fail):
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)')
        conn.execute("INSERT INTO settings VALUES ('uncommitted','application write')")

        def authorize(operation, *_):
            return (sqlite3.SQLITE_DENY if fail and operation == sqlite3.SQLITE_CREATE_TEMP_TRIGGER
                    else sqlite3.SQLITE_OK)

        conn.set_authorizer(authorize)
        try:
            with pytest.raises(sqlite3.DatabaseError) if fail else nullcontext():
                Revisions(conn)
        finally:
            conn.set_authorizer(None)
        assert conn.in_transaction
        assert conn.execute('SELECT value FROM settings').fetchone() == ('application write',)
        conn.rollback()
        assert not conn.execute('SELECT * FROM settings').fetchall()
