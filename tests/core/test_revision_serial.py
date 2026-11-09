"""Dynamic snapshot guards never copy a whole-library revision map."""
from contextlib import closing
import sqlite3

import pytest

from yt_playlist.core.db_revisions import Revisions


@pytest.fixture(params=['memory', 'file'])
def database(request):
    # These cases exercise live revision guards, not fresh-schema construction.
    return request.getfixturevalue('store' if request.param == 'memory' else 'file_backed_store')


def test_snapshot_pins_and_memo_reuse_only_seek_consumed_dependencies(database, monkeypatch):
    with database.conn:
        database.conn.executemany('INSERT INTO settings VALUES (?,?)',
                                 [(f'unrelated-{n}', 'x') for n in range(4000)])
    database.settings.set_setting('input', 'old')
    capture = database._snapshot_revisions.capture
    captures, calculations = [], []
    consumed = {('settings', 'key:input')}

    def bounded(dependencies=None):
        assert dependencies == consumed, 'Captured outside the consumed setting'
        captures.append(set(dependencies))
        return capture(dependencies)

    monkeypatch.setattr(database._snapshot_revisions, 'capture', bounded)

    def read(reader):
        def calculate():
            calculations.append(True)
            return {'value': reader.settings.get_setting('input')}
        before = len(captures)
        result = reader.memoized_read('serial-probe', calculate)
        assert len(captures) - before <= 1
        return result

    with database.read_snapshot() as snapshot:
        assert not captures
        result = read(snapshot.store)
        result['value'] = 'caller mutation'
        assert snapshot.publish(lambda: None)
    with database.read_snapshot() as snapshot:
        assert read(snapshot.store) == {'value': 'old'}
        database.settings.set_setting('unrelated-0', 'new')
        assert snapshot.publish(lambda: None)
    assert len(calculations) == 1
    # An equal-serial memo hit needs no revision seek: it describes the same
    # pinned inputs. A later serial must still check the exact consumed key.
    with database.read_snapshot() as snapshot:
        assert read(snapshot.store) == {'value': 'old'}
        assert len(calculations) == 1
        database.settings.set_setting('input', 'new')
        assert snapshot.store.settings.get_setting('input') == 'old'
        assert not snapshot.publish(lambda: pytest.fail('published changed input'))
    with database.read_snapshot() as snapshot:
        assert read(snapshot.store) == {'value': 'new'}
        assert snapshot.publish(lambda: None)
    assert len(calculations) == 2
    assert captures


def test_newer_memo_result_cannot_escape_into_older_pinned_reader(database):
    database.settings.set_setting('input', 'old')
    calls = []

    def read(reader):
        def calculate():
            calls.append(True)
            return reader.settings.get_setting('input')
        return reader.memoized_read('serial-order', calculate)

    with database.read_snapshot() as older:
        database.settings.set_setting('input', 'new')
        with database.read_snapshot() as newer:
            assert read(newer.store) == 'new'
        assert read(older.store) == 'old'
        assert not older.publish(lambda: pytest.fail('published an old view'))
    with database.read_snapshot() as current:
        assert read(current.store) == 'new'
    assert len(calls) == 3


def test_write_during_calculation_cannot_make_old_result_look_current(database):
    database.settings.set_setting('input', 'old')
    with database.read_snapshot() as snapshot:
        database.settings.set_setting('input', 'new')
        assert snapshot.store.memoized_read('during', lambda: snapshot.store.settings.get_setting('input')) == 'old'
    with database.read_snapshot() as snapshot:
        assert snapshot.store.memoized_read('during', lambda: snapshot.store.settings.get_setting('input')) == 'new'


@pytest.mark.parametrize('mutation', ['rollback', 'savepoint', 'unchanged', 'changed-back', 'delete'])
def test_trigger_stamps_follow_committed_changes_and_rollback(database, mutation):
    database.settings.set_setting('input', 'old')
    before = database.conn.total_changes
    original = database._snapshot_revisions.capture({('settings', 'key:input')})
    calls = []

    def read(reader):
        def calculate():
            calls.append(True)
            return reader.settings.get_setting('input')
        return reader.memoized_read('rollback-probe', calculate)

    with database.read_snapshot() as snapshot:
        assert read(snapshot.store) == 'old'
    if mutation == 'unchanged':
        database.settings.set_setting('input', 'old')
    elif mutation == 'savepoint':
        database.conn.execute('SAVEPOINT nested')
        database.conn.execute("UPDATE settings SET value='new' WHERE key='input'")
        database.conn.execute('ROLLBACK TO nested')
        database.conn.execute('RELEASE nested')
    elif mutation == 'rollback':
        database.conn.execute("UPDATE settings SET value='new' WHERE key='input'")
        database.conn.rollback()
    elif mutation == 'changed-back':
        database.settings.set_setting('input', 'new')
        database.settings.set_setting('input', 'old')
    else:
        with database.conn:
            database.conn.execute("DELETE FROM settings WHERE key='input'")
    after = database._snapshot_revisions.capture({('settings', 'key:input')})
    changed = mutation in ('changed-back', 'delete')
    if changed:
        assert all(before < stamp <= database.conn.total_changes for stamp in after.values())
    else:
        assert after == original
    with database.read_snapshot() as snapshot:
        assert read(snapshot.store) == (None if mutation == 'delete' else 'old')
    assert len(calls) == (2 if changed else 1)


def test_external_write_still_invalidates_memo_and_publication(file_backed_store):
    store = file_backed_store
    path = store._db_path
    store.settings.set_setting('input', 'old')
    with store.read_snapshot() as snapshot:
        assert snapshot.store.memoized_read('external', lambda: snapshot.store.settings.get_setting('input')) == 'old'
        with closing(sqlite3.connect(path)) as other, other:
            other.execute("UPDATE settings SET value='new' WHERE key='input'")
        assert not snapshot.publish(lambda: pytest.fail('ignored external commit'))
    with store.read_snapshot() as snapshot:
        assert snapshot.store.memoized_read('external', lambda: snapshot.store.settings.get_setting('input')) == 'new'


def test_revision_rebind_after_an_external_migration_retires_a_table(tmp_path):
    """Background schema cutover must release old counters and restore writes."""
    path = tmp_path / 'external-schema.db'
    reader = sqlite3.connect(path)
    try:
        reader.execute('PRAGMA journal_mode=WAL')
        reader.execute('CREATE TABLE genre_whitelist(name TEXT PRIMARY KEY)')
        reader.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        Revisions(reader)
        with closing(sqlite3.connect(path)) as writer, writer:
            writer.execute('DROP TABLE genre_whitelist')

        Revisions.unbind(reader)
        current = Revisions(reader)
        assert 'genre_whitelist' not in current.tables
        before = current.capture({('settings', 'key:after-upgrade')})
        with reader:
            reader.execute("INSERT INTO settings VALUES ('after-upgrade','saved')")
        assert current.capture({('settings', 'key:after-upgrade')}) != before
        assert reader.execute('SELECT value FROM settings').fetchone() == ('saved',)
    finally:
        reader.close()


@pytest.mark.parametrize('already_in_transaction', [False, True])
def test_guard_construction_allows_external_writes_and_rebinds_changed_schema(tmp_path, already_in_transaction):
    path = tmp_path / 'guard-construction.db'
    with closing(sqlite3.connect(path)) as conn, closing(sqlite3.connect(path, timeout=0)) as writer:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        changed, errors = [], []

        def concurrent_write(sql):
            if not changed and sql.startswith('CREATE TEMP TRIGGER'):
                changed.append(True)
                try:
                    with writer:
                        writer.execute("INSERT INTO settings VALUES ('concurrent','saved')")
                        writer.execute('CREATE TABLE added(value)')
                except Exception as exc:
                    errors.append(exc)

        if already_in_transaction:
            conn.execute('BEGIN')
        conn.set_trace_callback(concurrent_write)
        try:
            revisions = Revisions(conn)
        finally:
            conn.set_trace_callback(None)
        assert changed and not errors
        assert not conn.in_transaction
        assert conn.execute('SELECT value FROM settings').fetchone() == ('saved',)
        revisions.refresh()
        assert 'added' in revisions.tables
        before = revisions.capture({('added', '')})
        with conn:
            conn.execute("INSERT INTO added VALUES ('after binding')")
        assert revisions.capture({('added', '')}) != before


def test_revision_unbind_tolerates_a_trigger_retired_after_enumeration():
    """A controlled stale catalog entry must not prevent real SQLite cleanup."""
    from types import SimpleNamespace

    conn = sqlite3.connect(':memory:')
    try:
        conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        Revisions(conn)
        conn.execute('CREATE TEMP TRIGGER unrelated AFTER INSERT ON main.settings BEGIN SELECT 1; END')

        class StaleTriggerEnumeration:
            def execute(self, sql):
                result = conn.execute(sql)
                if sql.startswith('SELECT name FROM sqlite_temp_schema'):
                    rows = [('snapshot_revision_retired_insert',), *result.fetchall()]
                    return SimpleNamespace(fetchall=lambda: rows)
                return result

        # Only enumeration is controlled. Every DROP runs against real SQLite,
        # including the missing trigger followed by the surviving real triggers.
        Revisions.unbind(StaleTriggerEnumeration())
        assert list(conn.execute("SELECT name FROM sqlite_temp_schema WHERE type='trigger'")) == [('unrelated',)]
        assert conn.execute("SELECT 1 FROM sqlite_temp_schema WHERE name='snapshot_revisions'").fetchone() is None
        rebound = Revisions(conn)
        before = rebound.capture({('settings', 'key:after-upgrade')})
        with conn:
            conn.execute("INSERT INTO settings VALUES ('after-upgrade','saved')")
        assert rebound.capture({('settings', 'key:after-upgrade')}) != before
        assert conn.execute('SELECT value FROM settings').fetchone() == ('saved',)
    finally:
        conn.close()
