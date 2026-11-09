from contextlib import closing
import sqlite3

import pytest

from tests.store_fixture_support import bounded_consumer_schema, initialize_consumer
from yt_playlist.core.store import Store


@pytest.fixture(scope='module')
def _pristine_current_schema():
    with bounded_consumer_schema(()) as schema:
        yield schema


@pytest.fixture(autouse=True)
def disable_periodic_library_sync():
    """Memo invalidation exercises SQL admission, without an HTTP lifecycle."""


def test_tracked_view_reuses_only_unchanged_inputs_and_releases_admission(store):
    from yt_playlist.core.db_reads import MAX_READERS
    store.settings.set_setting('tracked-input', 'old')
    calls = []
    def read(reader):
        def calculate():
            calls.append(True)
            return [reader.settings.get_setting('tracked-input')]
        return reader.memoized_read('tracked-example', calculate)
    with store.read_view(tracked=True) as reader:
        assert read(reader) == ['old']
    store.settings.set_setting('unrelated', 'changed')
    with store.read_view(tracked=True) as reader:
        assert read(reader) == ['old']
        assert len(calls) == 1
    store.settings.set_setting('tracked-input', 'new')
    with pytest.raises(ValueError, match='stop'):
        with store.read_view(tracked=True) as reader:
            assert read(reader) == ['new']
            raise ValueError('stop')
    assert len(calls) == 2
    for _ in range(MAX_READERS):
        assert store._analytics_readers.acquire(blocking=False)
    assert not store._analytics_readers.acquire(blocking=False)
    for _ in range(MAX_READERS):
        store._analytics_readers.release()


@pytest.mark.parametrize('function', ['json_each', 'json_tree'])
def test_json_expansion_reuses_only_unchanged_input_values(store, function):
    store.settings.set_setting('json-input', '["first"]')
    calls = []

    def read(reader):
        def calculate():
            calls.append(True)
            payload = reader.settings.get_setting('json-input')
            return [row[0] for row in reader.conn.execute(
                f"SELECT value FROM {function}(?) WHERE type='text'", (payload,))]
        return reader.memoized_read('json-values', calculate)

    with store.read_view(tracked=True) as reader:
        assert read(reader) == ['first']
    store.settings.set_setting('unrelated', 'changed')
    with store.read_view(tracked=True) as reader:
        assert read(reader) == ['first']
    assert len(calls) == 1
    store.settings.set_setting('json-input', '["second"]')
    with store.read_view(tracked=True) as reader:
        assert read(reader) == ['second']
    assert len(calls) == 2


def test_composed_read_reuses_detached_results_only_within_its_pinned_view(store):
    store.settings.set_setting('input', 'old')
    calls = []
    def calculate(reader):
        def prepare():
            calls.append(True)
            return {'items': [reader.settings.get_setting('input')]}
        return reader.memoized_read('example', prepare)
    with store.read_view() as reader:
        first = calculate(reader)
        first['items'].append('caller mutation')
        store.settings.set_setting('input', 'new')
        with reader.read_view() as nested:
            assert calculate(nested) == {'items': ['old']}
        assert calculate(reader) == {'items': ['old']}
        assert len(calls) == 1
    with store.read_view() as reader:
        assert calculate(reader) == {'items': ['new']}
    assert len(calls) == 2


def test_pinned_read_reuse_never_borrows_newer_snapshot_inputs(store):
    store.settings.set_setting('input', 'old')
    calls = []

    def read(reader):
        def prepare():
            calls.append(True)
            return {'value': reader.settings.get_setting('input')}
        return reader.memoized_read('pinned-input', prepare)

    with store.read_snapshot() as older:
        assert read(older.store) == {'value': 'old'}
        store.settings.set_setting('input', 'new')
        assert read(older.store) == {'value': 'old'}
        assert len(calls) == 1
        with store.read_snapshot() as newer:
            assert read(newer.store) == {'value': 'new'}
            assert read(older.store) == {'value': 'old'}
            assert read(newer.store) == {'value': 'new'}
            assert not older.publish(lambda: pytest.fail('Stale inputs were published'))
    with store.read_snapshot() as newest:
        assert read(newest.store) == {'value': 'new'}


def test_preparation_cannot_evict_tracked_foreground_reads(store):
    store.settings.set_setting('input', 'old')
    calls = []
    def calculate(reader):
        def prepare():
            calls.append(True)
            return reader.settings.get_setting('input')
        return reader.memoized_read('interactive-result', prepare)
    with store.read_view(tracked=True) as reader:
        assert calculate(reader) == 'old'
    with store.read_snapshot() as snapshot:
        for index in range(64):
            snapshot.store.memoized_read(('preparation', index),
                lambda: snapshot.store.settings.get_setting('input'))
    with store.read_view(tracked=True) as reader:
        assert calculate(reader) == 'old'
    assert len(calls) == 1
    store.settings.set_setting('input', 'new')
    with store.read_view(tracked=True) as reader:
        assert calculate(reader) == 'new'
    assert len(calls) == 2


def test_cache_hit_keeps_publication_dependencies_and_scopes_settings(store):
    store.settings.set_setting("input", "old")
    calls = []
    def calculate(reader):
        def prepare():
            calls.append(True)
            return reader.settings.get_setting("input")
        return reader.memoized_read("example", prepare)
    with store.read_snapshot() as snapshot:
        assert calculate(snapshot.store) == "old"
    store.settings.set_setting("unrelated", "new")
    with store.read_snapshot() as snapshot:
        assert calculate(snapshot.store) == "old"
        assert len(calls) == 1
        store.settings.set_setting("input", "new")
        assert not snapshot.publish(lambda: None)
    with store.read_snapshot() as snapshot:
        assert calculate(snapshot.store) == "new"
    assert len(calls) == 2


def test_nested_captures_retain_overlapping_dependencies(store):
    store.settings.set_setting("input", "old")
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        reader.settings.get_setting("input")
        reader.memoized_read("inner", lambda: reader.settings.get_setting("input"))
        assert reader.memoized_read("outer", lambda: reader.memoized_read(
            "inner", lambda: reader.settings.get_setting("input"))) == "old"
    store.settings.set_setting("input", "new")
    with store.read_snapshot() as snapshot:
        reader = snapshot.store
        assert reader.memoized_read("outer", lambda: reader.memoized_read(
            "inner", lambda: reader.settings.get_setting("input"))) == "new"


def test_external_commit_during_reader_pinning_cannot_reuse_old_result(
        tmp_path, _pristine_current_schema, monkeypatch):
    path = tmp_path / "state.db"
    store = Store(path)
    store.conn.execute('PRAGMA synchronous=OFF')
    initialize_consumer(store, _pristine_current_schema)
    store.settings.set_setting("input", "old")
    def calculate(reader):
        return reader.memoized_read("example", lambda: reader.settings.get_setting("input"))
    try:
        with store.read_snapshot() as snapshot:
            assert calculate(snapshot.store) == "old"
        original = Store._configure_connection
        written = []
        def configure(reader):
            original(reader)
            def on_pin(sql):
                if sql == "SELECT 1 FROM settings LIMIT 1" and not written:
                    with closing(sqlite3.connect(path)) as other, other:
                        other.execute("UPDATE settings SET value='new' WHERE key='input'")
                    written.append(True)
            reader.conn.set_trace_callback(on_pin)
        monkeypatch.setattr(Store, "_configure_connection", configure)
        with store.read_snapshot() as snapshot:
            assert calculate(snapshot.store) == "new"
        assert written
    finally:
        store.conn.close()
