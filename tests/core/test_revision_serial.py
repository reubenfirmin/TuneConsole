"""Dynamic snapshot guards never copy a whole-library revision map."""
import sqlite3

import pytest

from yt_playlist.core.store import Store


@pytest.fixture(params=['memory', 'file'])
def database(request, tmp_path):
    store = Store(':memory:' if request.param == 'memory' else tmp_path / 'state.db')
    store.init_schema()
    yield store
    store.conn.close()


def test_snapshot_pins_and_memo_reuse_only_seek_consumed_dependencies(database, monkeypatch):
    with database.conn:
        database.conn.executemany('INSERT INTO settings VALUES (?,?)',
                                 [(f'unrelated-{n}', 'x') for n in range(4000)])
    database.settings.set_setting('input', 'old')
    capture = database._snapshot_revisions.capture
    captures, calculations = [], []

    def bounded(dependencies=None):
        assert dependencies is not None, 'Copied the whole revision map'
        captures.append(set(dependencies))
        return capture(dependencies)

    monkeypatch.setattr(database._snapshot_revisions, 'capture', bounded)

    def read(reader):
        def calculate():
            calculations.append(True)
            return {'value': reader.settings.get_setting('input')}
        return reader.memoized_read('serial-probe', calculate)

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
    assert captures == [{('settings', 'key:input')}] * 3


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


def test_external_write_still_invalidates_memo_and_publication(tmp_path):
    path = tmp_path / 'state.db'
    store = Store(path)
    store.init_schema()
    store.settings.set_setting('input', 'old')
    try:
        with store.read_snapshot() as snapshot:
            assert snapshot.store.memoized_read('external', lambda: snapshot.store.settings.get_setting('input')) == 'old'
            with sqlite3.connect(path) as other:
                other.execute("UPDATE settings SET value='new' WHERE key='input'")
            assert not snapshot.publish(lambda: pytest.fail('ignored external commit'))
        with store.read_snapshot() as snapshot:
            assert snapshot.store.memoized_read('external', lambda: snapshot.store.settings.get_setting('input')) == 'new'
    finally:
        store.conn.close()
