"""Failed repository units release only the transactions they started.

FULL uses a private SQLite page cap; BUSY uses a private DELETE-journal reader.
Neither fault fills the filesystem or touches an installed library.
"""
from contextlib import closing, nullcontext
import errno
import json
import sqlite3

import pytest

from yt_playlist.core.db_diagnostics import DiagnosticConnection
from yt_playlist.core.db_writes import atomic_write
from yt_playlist.controllers.group_preparation import PROGRESS, report
from yt_playlist.repos.base import Repo, synchronized


@pytest.fixture
def writes(file_backed_store):
    store = file_backed_store
    account = store.accounts.upsert_account('Recovery', None, True)
    store.settings.set_setting('lastfm_api_key', 'previous')

    def write(kind, value):
        if kind == 'event':
            return store.player_events.record_player_event(
                account, 'state', value, 10, 180, None, None, 100)
        if kind == 'delete':
            return store.settings.delete_setting('lastfm_api_key')
        return store.settings.set_setting('lastfm_api_key', value)

    return store, account, write


def assert_recovered(store):
    assert not store.conn.in_transaction
    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('lastfm_api_key') == 'previous'
        assert snapshot.store.player_events.player_events_since(0) == []
    # A newly admitted write must work before any later ordinary event/setting.
    with atomic_write(store):
        store.conn.execute("INSERT INTO settings(key,value) VALUES ('recovered','yes')")


def assert_no_later_commit(store, write):
    write('event', 'later-event')
    store.settings.set_setting('later-setting', 'confirmed')
    with closing(sqlite3.connect(store._db_path.as_uri() + '?mode=ro', uri=True)) as observer:
        assert observer.execute('SELECT video_id FROM player_events').fetchall() == [('later-event',)]
        assert observer.execute("SELECT value FROM settings WHERE key='lastfm_api_key'").fetchone() == ('previous',)
        assert not observer.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('kind', ['event', 'setting'])
def test_actual_full_during_execute_releases_failed_unit(writes, kind):
    store, account, write = writes
    limit = store.conn.execute('PRAGMA max_page_count').fetchone()[0]
    pages = store.conn.execute('PRAGMA page_count').fetchone()[0]
    store.conn.execute(f'PRAGMA max_page_count={pages}')
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            if kind == 'event':
                store.player_events.record_player_event(account, 'state', 'failed-event', 10, 180,
                    None, json.dumps({'probe': 'x' * 1048576}), 100)
            else:
                write(kind, 'x' * 1048576)
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_FULL
    finally:
        store.conn.execute(f'PRAGMA max_page_count={limit}')
    assert_recovered(store)
    assert_no_later_commit(store, write)


def test_actual_full_keeps_native_group_progress_recovery(writes):
    store, _account, write = writes
    report(store.settings, 'ready', 5, reason='previous')
    previous = store.settings.get_setting(PROGRESS)
    limit = store.conn.execute('PRAGMA max_page_count').fetchone()[0]
    pages = store.conn.execute('PRAGMA page_count').fetchone()[0]
    store.conn.execute(f'PRAGMA max_page_count={pages}')
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            report(store.settings, 'deferred', 10, reason='x' * 1048576)
        assert caught.value.sqlite_errorcode == sqlite3.SQLITE_FULL
    finally:
        store.conn.execute(f'PRAGMA max_page_count={limit}')
    assert_recovered(store)
    assert_no_later_commit(store, write)
    assert store.settings.get_setting(PROGRESS) == previous


@pytest.mark.parametrize('kind', ['event', 'setting', 'delete'])
def test_actual_busy_during_commit_does_not_leave_failed_action_pending(writes, kind):
    store, _account, write = writes
    assert store.conn.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
    store.conn.execute('PRAGMA busy_timeout=0')
    with closing(sqlite3.connect(store._db_path)) as blocker:
        blocker.execute('BEGIN')
        blocker.execute('SELECT count(*) FROM player_events').fetchone()
        try:
            with pytest.raises(sqlite3.OperationalError) as caught:
                write(kind, 'failed-action')
            assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        finally:
            blocker.rollback()
    assert_recovered(store)
    assert_no_later_commit(store, write)


@pytest.mark.parametrize('kind', ['event', 'setting', 'delete'])
@pytest.mark.parametrize('outer_atomic', [False, True])
def test_controlled_full_before_commit_rolls_back_without_replay(writes, monkeypatch, kind, outer_atomic):
    store, _account, write = writes
    finish = DiagnosticConnection._finish
    attempts = []

    def full(connection, sql, target, *args):
        if connection is store.conn and sql == 'COMMIT':
            attempts.append(sql)
            error = sqlite3.OperationalError('controlled FULL before commit')
            error.sqlite_errorcode = sqlite3.SQLITE_FULL
            raise error
        return finish(connection, sql, target, *args)

    with monkeypatch.context() as patch:
        patch.setattr(DiagnosticConnection, '_finish', full)
        with pytest.raises(sqlite3.OperationalError, match='controlled FULL'):
            with store._lock, store.conn.atomic() if outer_atomic else nullcontext():
                write(kind, 'failed-action')
    assert attempts == ['COMMIT']
    assert_recovered(store)
    assert_no_later_commit(store, write)


class Cancelled(BaseException):
    pass


class InterruptedRepo(Repo):
    @synchronized
    def write_then_fail(self, error):
        self.conn.execute("INSERT INTO settings(key,value) VALUES ('interrupted','pending')")
        raise error

    @synchronized
    def nested(self, error):
        self.write_then_fail(error)


@pytest.mark.parametrize('cancelled', [False, True])
def test_nested_failed_unit_cleans_up_before_releasing_its_lock(writes, monkeypatch, cancelled):
    store, _account, write = writes
    error = Cancelled() if cancelled else OSError(errno.ENOSPC, 'publication unavailable')
    rollback = DiagnosticConnection.rollback

    def locked_rollback(connection):
        if connection is store.conn:
            assert store._lock.held_by_current_thread
        return rollback(connection)

    monkeypatch.setattr(DiagnosticConnection, 'rollback', locked_rollback)
    with pytest.raises(type(error)) as caught:
        InterruptedRepo(store).nested(error)
    assert caught.value is error
    assert_recovered(store)
    assert store.settings.get_setting('interrupted') is None
    assert_no_later_commit(store, write)


@pytest.mark.parametrize('owner', ['transaction', 'connection-context', 'atomic'])
def test_failed_borrower_preserves_caller_transaction_and_guards(writes, owner):
    store, _account, _write = writes
    error = OSError(errno.ENOSPC, 'publication unavailable')
    with store._lock:
        if owner != 'atomic':
            store.conn.execute('BEGIN IMMEDIATE')
        scope = (store.conn.atomic() if owner == 'atomic' else
                 store.conn if owner == 'connection-context' else nullcontext())
        with scope:
            store.conn.execute("INSERT INTO settings(key,value) VALUES ('caller','pending')")
            with pytest.raises(OSError) as caught:
                InterruptedRepo(store).nested(error)
            assert caught.value is error
            assert store.conn.in_transaction
            assert store.settings.get_setting('caller') == 'pending'
            with pytest.raises(RuntimeError, match='Cannot snapshot uncommitted changes'):
                with store.read_snapshot():
                    pass
            with pytest.raises(RuntimeError, match='An atomic unit requires an idle connection'):
                with store.conn.atomic():
                    pass
            # The caller alone decides how to settle this transaction.
            store.conn.execute("DELETE FROM settings WHERE key='interrupted'")
        if owner == 'transaction':
            store.conn.commit()
    assert not store.conn.in_transaction
    assert store.settings.get_setting('caller') == 'pending'
    assert store.settings.get_setting('interrupted') is None


@pytest.mark.parametrize('ordered', [False, True])
def test_failed_snapshot_publication_recovers_without_replaying_committed_steps(writes, monkeypatch, ordered):
    store, _account, write = writes
    finish = DiagnosticConnection._finish

    def full(connection, sql, target, *args):
        if connection is store.conn and sql == 'COMMIT':
            error = sqlite3.OperationalError('controlled publication commit FULL')
            error.sqlite_errorcode = sqlite3.SQLITE_FULL
            raise error
        return finish(connection, sql, target, *args)

    with store.read_snapshot() as snapshot:
        def publish():
            if ordered:
                store.settings.set_setting('completed-step', 'retained')
            else:
                store.conn.execute("INSERT INTO settings(key,value) VALUES ('completed-step','pending')")
            # Fail only the unsettled unit. Ordered publication keeps earlier
            # confirmed commits; atomic publication rolls back the whole unit.
            patch.setattr(DiagnosticConnection, '_finish', full)
            if ordered:
                write('setting', 'failed-publication')
            else:
                store.conn.execute("UPDATE settings SET value='failed-publication' WHERE key='lastfm_api_key'")

        with monkeypatch.context() as patch:
            # Keep the fault bounded to publication, including its cleanup.
            with pytest.raises(sqlite3.OperationalError, match='publication commit FULL'):
                (snapshot.publish_ordered if ordered else snapshot.publish)(publish)
    assert store.settings.get_setting('completed-step') == ('retained' if ordered else None)
    assert_recovered(store)
    assert_no_later_commit(store, write)
