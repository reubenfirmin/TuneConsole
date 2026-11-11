"""Ordinary consumer fixtures preserve fresh data and independent ownership."""
import json
import sqlite3
from contextlib import closing

import pytest

from tests.home_fixture_support import browser_store
from tests.store_fixture_support import mock_snapshot_coordinator
from yt_playlist.core import group_preview_work
from yt_playlist.core.store import Store


def _logical_state(conn):
    result = {}
    for name, sql in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        quoted = '"' + name.replace('"', '""') + '"'
        cursor = conn.execute('SELECT * FROM ' + quoted)
        columns = [column[0] for column in cursor.description]
        rows = []
        for row in cursor:
            values = dict(zip(columns, tuple(row)))
            if name in ('schema_migrations', 'schema_changes'):
                values.pop('applied_at', None)
            rows.append(json.dumps(values, sort_keys=True, default=lambda value: value.hex()))
        result[name] = (sql, columns, sorted(rows))
    return result


@pytest.fixture(scope='module')
def fresh_reference():
    fresh = Store(':memory:')
    try:
        fresh.init_schema()
        fresh.conn.execute('PRAGMA query_only=ON')
        yield fresh
    finally:
        fresh.conn.close()


@pytest.fixture
def consumer_memory(consumer_store):
    return consumer_store()


@pytest.fixture
def consumer_file(consumer_store, tmp_path):
    return consumer_store(tmp_path / 'consumer.db')


@pytest.mark.parametrize('fixture_name', ['store', 'file_backed_store', 'browser_store',
                                       'consumer_memory', 'consumer_file'])
def test_consumer_fixture_matches_a_genuine_fresh_bootstrap(
        request, fixture_name, _pristine_current_schema, fresh_reference):
    store = request.getfixturevalue(fixture_name)
    fresh = fresh_reference
    expected = _logical_state(fresh.conn)
    assert _logical_state(store.conn) == expected
    assert _logical_state(_pristine_current_schema) == expected
    assert list(store.conn.execute(
        "SELECT type,name,sql FROM sqlite_schema ORDER BY type,name")) == list(fresh.conn.execute(
        "SELECT type,name,sql FROM sqlite_schema ORDER BY type,name"))
    assert list(store.conn.execute(
        "SELECT type,name,sql FROM sqlite_temp_schema ORDER BY type,name")) == list(fresh.conn.execute(
        "SELECT type,name,sql FROM sqlite_temp_schema ORDER BY type,name"))
    assert vars(store._snapshot_revisions).keys() == vars(fresh._snapshot_revisions).keys()
    assert store._snapshot_revisions.conn is store.conn
    for name in vars(fresh._snapshot_revisions).keys() - {'conn', 'schema_version'}:
        actual, expected = getattr(store._snapshot_revisions, name), getattr(fresh._snapshot_revisions, name)
        assert actual == expected
        if isinstance(actual, (dict, list, set)):
            assert actual is not expected
    assert store._snapshot_revisions.schema_version == store.conn.execute(
        'PRAGMA main.schema_version').fetchone()[0]
    assert store.__dict__.keys() == fresh.__dict__.keys()
    assert {name: value for name, value in store.__dict__.items()
            if name.startswith('_') and isinstance(value, bool)} == {
        name: value for name, value in fresh.__dict__.items()
        if name.startswith('_') and isinstance(value, bool)}
    assert store._evidence_key != fresh._evidence_key
    assert not store.conn.in_transaction
    assert store.conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    if store._db_path is not None:
        assert store.conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        _pristine_current_schema.execute(
            "INSERT INTO settings(key,value) VALUES ('template_write','forbidden')")


@pytest.mark.parametrize('value', ['first consumer', 'second consumer'])
@pytest.mark.parametrize('fixture_name', ['store', 'file_backed_store', 'browser_store',
                                       'consumer_memory', 'consumer_file'])
def test_consumer_mutations_never_reach_another_fixture(request, fixture_name, _pristine_current_schema, value,
                                                      consumer_store, tmp_path, monkeypatch):
    store = request.getfixturevalue(fixture_name)
    assert store.settings.get_setting('fixture_private_value') is None
    assert not store.conn.execute(
        "SELECT 1 FROM youtube_items WHERE video_id='fixture_private_track'").fetchone()
    published = []
    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('fixture_private_value') is None
        store.settings.set_setting('fixture_private_value', value)
        store.tracks.upsert_track('fixture_private_track', 'Private song', 'Private artist', 'Private album', 200)
        assert not snapshot.publish(lambda: published.append(True))
    assert not published
    with store.read_snapshot() as snapshot:
        assert snapshot.store.settings.get_setting('fixture_private_value') == value
        assert snapshot.publish(lambda: published.append(True))
    assert published == [True]
    assert store.settings.get_setting('fixture_private_value') == value
    assert not _pristine_current_schema.execute(
        "SELECT 1 FROM settings WHERE key='fixture_private_value'").fetchone()
    assert not _pristine_current_schema.execute(
        "SELECT 1 FROM youtube_items WHERE video_id='fixture_private_track'").fetchone()
    if fixture_name.startswith('consumer_'):
        peer = consumer_store(':memory:' if store._db_path is None else tmp_path / 'peer.db')
        assert peer.conn is not store.conn and peer._evidence_key != store._evidence_key
        assert peer.settings.get_setting('fixture_private_value') is None
        assert not peer.conn.execute(
            "SELECT 1 FROM youtube_items WHERE video_id='fixture_private_track'").fetchone()
    if fixture_name == 'consumer_file':
        with mock_snapshot_coordinator(store, monkeypatch):
            with store.read_snapshot() as snapshot:
                assert snapshot._schema == store.conn.execute('PRAGMA main.schema_version').fetchone()[0]
                assert snapshot.is_current()
                with store.conn:
                    store.conn.execute('INSERT INTO group_preview_work '
                        '(singleton,token,schema_version,started_at) VALUES (1,?,?,1)',
                        ('private-preview', snapshot._schema))
                assert not snapshot.is_current()
                assert snapshot.is_current(allow_stale_tables=group_preview_work.TABLES)
                store.settings.set_setting('fixture_private_value', 'changed input')
                assert not snapshot.is_current(allow_stale_tables=group_preview_work.TABLES)
            with store.read_snapshot() as snapshot:
                with closing(sqlite3.connect(store._db_path)) as other:
                    other.execute("INSERT INTO settings VALUES ('external-fixture-write','changed')")
                    other.commit()
                assert not snapshot.is_current(allow_stale_tables=group_preview_work.TABLES)
            with store.read_snapshot() as snapshot:
                with store.conn:
                    store.conn.execute('CREATE TABLE fixture_schema_change(value TEXT)')
                assert not snapshot.is_current(allow_stale_tables=group_preview_work.TABLES)
