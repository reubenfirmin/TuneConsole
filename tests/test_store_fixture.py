"""Ordinary consumer fixtures preserve fresh data and independent ownership."""
import json
import sqlite3

import pytest

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


def test_consumer_fixture_matches_a_genuine_fresh_bootstrap(store, _pristine_current_schema):
    fresh = Store(':memory:')
    try:
        fresh.init_schema()
        expected = _logical_state(fresh.conn)
        assert _logical_state(store.conn) == expected
        assert _logical_state(_pristine_current_schema) == expected
        assert store._evidence_key != fresh._evidence_key
        assert not store.conn.in_transaction
        assert store.conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            _pristine_current_schema.execute(
                "INSERT INTO settings(key,value) VALUES ('template_write','forbidden')")
    finally:
        fresh.conn.close()


@pytest.mark.parametrize('value', ['first consumer', 'second consumer'])
def test_consumer_mutations_never_reach_another_fixture(store, _pristine_current_schema, value):
    assert store.settings.get_setting('fixture_private_value') is None
    assert not store.conn.execute(
        "SELECT 1 FROM youtube_items WHERE video_id='fixture_private_track'").fetchone()
    store.settings.set_setting('fixture_private_value', value)
    store.tracks.upsert_track('fixture_private_track', 'Private song', 'Private artist', 'Private album', 200)
    assert store.settings.get_setting('fixture_private_value') == value
    assert not _pristine_current_schema.execute(
        "SELECT 1 FROM settings WHERE key='fixture_private_value'").fetchone()
    assert not _pristine_current_schema.execute(
        "SELECT 1 FROM youtube_items WHERE video_id='fixture_private_track'").fetchone()
